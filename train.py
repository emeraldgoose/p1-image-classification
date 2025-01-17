import os
from re import X
import numpy as np
import pandas as pd
import argparse
import json
import random
from importlib import import_module
from timm.models.byobnet import num_groups
from timm.models.efficientnet import EfficientNet
from tqdm import tqdm

from torch import nn, optim
from omegaconf import OmegaConf
import torch
import torchvision
from torchvision import transforms
from torch.utils.data import DataLoader
from sklearn.model_selection import StratifiedKFold
from torch.cuda.amp import autocast_mode, grad_scaler

from sklearn.metrics import f1_score

from data_loader.dataset import MaskDataset
from model.model import VIT, EfficientNet, ResNet_Mask,ResNet_Gender,ResNet_Age
import data_transform

def set_seed(random_seed):
    torch.manual_seed(random_seed)
    torch.cuda.manual_seed(random_seed)
    torch.cuda.manual_seed_all(random_seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(random_seed)
    random.seed(random_seed)

def rand_bbox(size, lam):
    H = size[2]
    W = size[3]
    cut_rat = np.sqrt(1. - lam)
    cut_w = int(W * cut_rat)
    cut_h = int(H * cut_rat)

    cx = np.random.randn() + W//2
    cy = np.random.randn() + H//2

    # 패치의 4점
    bbx1 = np.clip(cx - cut_w // 2, 0, W//2)
    bby1 = np.clip(cy - cut_h // 2, 0, H)
    bbx2 = np.clip(cx + cut_w // 2, 0, W//2)
    bby2 = np.clip(cy + cut_h // 2, 0, H)

    return int(bbx1), int(bby1), int(bbx2), int(bby2)


def train_one_epoch(model, train_loader, criterion, optimizer, device, scaler, cutmix):
    epoch_f1 = 0.0
    n_iter = 0.0
    train_loss = 0.0
    train_acc = 0.0
    
    for batch_index, (x_batch, y_batch) in enumerate(tqdm(train_loader)):
        x_batch = torch.stack(list(x_batch), dim=0).to(device)
        y_batch = torch.tensor(list(y_batch)).to(device)

        optimizer.zero_grad()
        with autocast_mode.autocast():
            if cutmix:
                if np.random.random() > 0.5: # Cutmix
                    random_index = torch.randperm(x_batch.size()[0])
                    target_a = y_batch
                    targeb_b = y_batch[random_index]

                    lam = np.random.beta(1.0, 1.0)
                    bbx1, bby1, bbx2, bby2 = rand_bbox(x_batch.size(), lam)

                    x_batch[:, :, bbx1:bbx2, bby1:bby2] = x_batch[random_index, :, bbx1:bbx2, bby1:bby2]
                    lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (x_batch.size()[-1] * x_batch.size()[-2]))

                    logits = model(x_batch.float())
                    loss = criterion(logits, target_a) * lam + criterion(logits, targeb_b) * (1. - lam)

                    _, preds = torch.max(logits, 1)
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()

                else:
                    logits = model(x_batch.float())
                    loss = criterion(logits, y_batch)

                    _, preds = torch.max(logits, 1)
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()

            else:
                logits = model(x_batch.float())
                loss = criterion(logits, y_batch)
                
                _, preds = torch.max(logits, 1)
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            

        train_loss += loss.item() * x_batch.size(0)
        train_acc += torch.sum(preds == y_batch.data)
        epoch_f1 += f1_score(y_batch.cpu().numpy(), preds.cpu().numpy(), average='macro')
        n_iter += 1
        
    epoch_loss = train_loss / len(train_loader.dataset)
    epoch_acc = train_acc / len(train_loader.dataset)
    epoch_f1 = epoch_f1/n_iter

    if cutmix:
        return epoch_loss, None, None,

    return epoch_loss, epoch_acc, epoch_f1


def validation(model, test_loader, criterion, device):
    val_loss_items = []
    val_acc_items = []
    val_f1_score = 0.0
    v_iter = 0

    for val_batch in test_loader:
        inputs, labels = val_batch
        inputs = inputs.to(device)
        labels = labels.to(device)

        outs = model(inputs)
        preds = torch.argmax(outs, dim=-1)

        loss_item = criterion(outs, labels).item()
        acc_item = (labels == preds).sum().item()

        val_loss_items.append(loss_item)
        val_acc_items.append(acc_item)
        val_f1_score += f1_score(labels.cpu().numpy(), preds.cpu().numpy(), average='macro')
        v_iter += 1

    val_loss = np.sum(val_loss_items) / len(test_loader)
    val_f1 = np.sum(val_f1_score) / v_iter

    return val_loss, val_f1


def get_model(model_name, num_classes, device):
    module = getattr(import_module('model.model'), model_name)
    model = module(num_classes)
    model.to(device)

    return model


def main(config, model_name, checkpoint=False):
    # Set seed
    set_seed(42)

    # PyTorch version & Check using cuda
    print ("PyTorch version:[%s]."%(torch.__version__))
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') #'cuda:0'
    print ("device:[%s]."%(device))
    num_classes = config[model_name]['num_classes']
    epochs = config[model_name]['epochs'] 
    target = config[model_name]['target']
    dir_ = config[model_name]['dir'] # 모델 별 이미지 경로
    cutmix = config[model_name]['cutmix'] 
    batch_size = config[model_name]['batch_size'] 
    lr_rate = config[model_name]['lr_rate'] 

    kfold = StratifiedKFold(n_splits=5, shuffle=True)
    train_df = pd.read_csv("/opt/ml/image-classification-level1-20/mask-classification/path_and_label.csv")
    
    x_train = train_df['path'].to_numpy()
    y_train = train_df[target].to_numpy()
    
    # /opt/ml/best_model
    path = '/opt/ml/image-classification-level1-20/mask-classification/model_saved' # checkpoint path

    for fold, (train_index, test_index) in enumerate(kfold.split(x_train, y_train)):
        print(str(fold+1) + "/5 Fold :")
        torch.cuda.empty_cache()
        transform_module = getattr(data_transform, config[model_name]["transform"])
        transform = transform_module()

        model = get_model(model_name=model_name, num_classes=num_classes, device=device)

        criterion = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(model.parameters(), lr=lr_rate)
        scaler = grad_scaler.GradScaler()

        x_train_fold = x_train[train_index]
        x_test_fold = x_train[test_index]

        train = MaskDataset(dir_, x_train_fold, transform, target)
        test = MaskDataset(dir_, x_test_fold, transform, target)
        train_loader = DataLoader(train, batch_size = batch_size, shuffle = True)
        test_loader = DataLoader(test, batch_size = batch_size, shuffle = True)
        
        # train        
        best_f1 = 0.
        for epoch in range(epochs):
            model.train()
            loss, acc, f1 = train_one_epoch(model, train_loader, criterion, optimizer, device, scaler, cutmix=cutmix)
            print(f'epoch : {epoch}, loss : {loss}, acc : {acc}, f1 : {f1}')

        # validation
        with torch.no_grad():
            model.eval()
            val_loss, val_f1 = validation(model, test_loader, criterion, device)
            if val_f1 > best_f1:
                model_saved = model_name + "model_saved" + str(fold) + ".pt"
                torch.save(model.state_dict(), os.path.join(path,model_saved))
                best_f1 = val_f1
            print(f'val loss : {val_loss:.3f}, val f1 : {val_f1:.3f}')

        del model, optimizer, test_loader, scaler

if __name__ == '__main__':
    # argparser
    parser = argparse.ArgumentParser(description='PyTorch Template')
    parser.add_argument('--model', type=str, required=False)
    args = parser.parse_args()
    model_name = args.model
    config = OmegaConf.load("/opt/ml/image-classification-level1-20/mask-classification/config.json")
    main(config, model_name)
