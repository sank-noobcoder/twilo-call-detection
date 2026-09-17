"""
twilio-voice-auth-project/voice_auth_service/model_def.py

Model architecture only -- same as your chat app's version. Copied here
so this project is fully standalone and doesn't depend on the other
project's files.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class ConvBlock(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.conv = nn.Conv2d(in_ch, out_ch, kernel_size=3, padding=1)
        self.bn = nn.BatchNorm2d(out_ch)
        self.pool = nn.MaxPool2d((2, 2))

    def forward(self, x):
        return self.pool(F.relu(self.bn(self.conv(x))))


class AttentivePooling(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.attn = nn.Sequential(
            nn.Linear(dim, dim // 2),
            nn.Tanh(),
            nn.Linear(dim // 2, 1)
        )

    def forward(self, x):  # x: (B, T, dim)
        weights = torch.softmax(self.attn(x), dim=1)
        pooled = torch.sum(weights * x, dim=1)
        return pooled, weights


class VoiceAuthenticityNet(nn.Module):
    def __init__(self, n_mels=80, cnn_channels=(32, 64, 128), lstm_hidden=128,
                 num_classes=2, dropout=0.3):
        super().__init__()
        in_ch = 1
        blocks = []
        for out_ch in cnn_channels:
            blocks.append(ConvBlock(in_ch, out_ch))
            in_ch = out_ch
        self.cnn = nn.Sequential(*blocks)

        freq_after = n_mels // (2 ** len(cnn_channels))
        self.lstm_input_dim = cnn_channels[-1] * freq_after

        self.lstm = nn.LSTM(
            input_size=self.lstm_input_dim, hidden_size=lstm_hidden,
            num_layers=2, batch_first=True, bidirectional=True, dropout=dropout
        )

        self.pool = AttentivePooling(lstm_hidden * 2)
        self.classifier = nn.Sequential(
            nn.Linear(lstm_hidden * 2, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, num_classes)
        )

    def forward(self, x):
        x = x.unsqueeze(1)
        x = self.cnn(x)
        B, C, Fp, Tp = x.shape
        x = x.permute(0, 3, 1, 2).reshape(B, Tp, C * Fp)
        x, _ = self.lstm(x)
        pooled, attn_weights = self.pool(x)
        logits = self.classifier(pooled)
        return logits, attn_weights
