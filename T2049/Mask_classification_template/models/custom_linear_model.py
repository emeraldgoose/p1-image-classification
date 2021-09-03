import torch
import torch.nn as nn
import torch.nn.functional as F
from models.resnext50 import Resnext50, Resnext50_2, Resnext50_3, initialize_weights




class CustomLinearModel(nn.Module):
    def __init__(self):
        super().__init__()
        device = device = torch.device('cuda' if torch.cuda.is_available() else "cpu")
        self.model_age = Resnext50_3()
        self.model_age = self.model_age()
        checkpoint_age = torch.load(f"/opt/ml/code/custom/best_model/age-2021-9-2-2-46_bs-64_lr-0.005.pt")
        self.model_age.load_state_dict(checkpoint_age['state_dict'])
        for param in self.model_age.parameters():
            param.requires_grad = False
        self.model_age.to(device)


        self.model_gender = Resnext50_2()
        self.model_gender = self.model_gender()
        checkpoint_gender = torch.load(f"/opt/ml/code/custom/best_model/gender-2021-9-2-3-33_bs-64_lr-0.005.pt")
        self.model_gender.load_state_dict(checkpoint_gender['state_dict'])
        for param in self.model_gender.parameters():
            param.requires_grad = False
        self.model_gender.to(device)


        self.model_mask = Resnext50_3()
        self.model_mask = self.model_mask()
        checkpoint_mask = torch.load(f"/opt/ml/code/custom/best_model/mask-mtcnn-f1_score_0.8-cutmix-2021-8-30_bs-64_lr-0.0005.pt")
        self.model_mask.load_state_dict(checkpoint_mask['state_dict'])
        for param in self.model_mask.parameters():
            param.requires_grad = False
        self.model_mask.to(device)


        self.model_linear = nn.Linear(8, 18)

        initialize_weights(self.model_linear)

        self.model_linear_2 = nn.Linear(18, 18)

        initialize_weights(self.model_linear_2)

    
    def forward(self, x):
        y1 = self.model_age(x)
        y2 = self.model_gender(x)
        y3 = self.model_mask(x)


        y = torch.cat([y1, y2, y3], dim=1)
        y = self.model_linear(y)
        y = F.relu(y)
        y = F.dropout(y, p=0.5)
        y = self.model_linear_2(y)
        return y