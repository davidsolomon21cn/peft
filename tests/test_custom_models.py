#!/usr/bin/env python3

# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import os
import platform
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from functools import partial

import pytest
import torch
from safetensors.torch import load_file as safe_load_file
from torch import nn
from transformers import AutoModelForCausalLM, AutoModelForSequenceClassification
from transformers.pytorch_utils import Conv1D

from peft import (
    AdaLoraConfig,
    BOFTConfig,
    BoneConfig,
    C3AConfig,
    FourierFTConfig,
    HRAConfig,
    IA3Config,
    LNTuningConfig,
    LoHaConfig,
    LoKrConfig,
    LoraConfig,
    MissConfig,
    OFTConfig,
    PeftModel,
    RandLoraConfig,
    ShiraConfig,
    TaskType,
    TrainableTokensConfig,
    VBLoRAConfig,
    VeraConfig,
    get_peft_model,
)
from peft.tuners.tuners_utils import BaseTunerLayer
from peft.utils import AuxiliaryTrainingWrapper, infer_device

from .testing_common import PeftCommonTester
from .testing_utils import get_state_dict, require_non_cpu


# MLP is a vanilla FF network with only linear layers
# EmbConv1D has an embedding and a Conv1D layer
# Conv2D has a Conv2D layer
TEST_CASES = [
    ########
    # LoRA #
    ########
    ("Vanilla MLP 1 LoRA", "MLP", LoraConfig, {"target_modules": "lin0"}),
    ("Vanilla MLP 2 LoRA", "MLP", LoraConfig, {"target_modules": ["lin0"]}),
    ("Vanilla MLP 3 LoRA", "MLP", LoraConfig, {"target_modules": ["lin1"]}),
    ("Vanilla MLP 4 LoRA", "MLP", LoraConfig, {"target_modules": ["lin0", "lin1"]}),
    ("Vanilla MLP 5 LoRA", "MLP", LoraConfig, {"target_modules": ["lin0"], "modules_to_save": ["lin1"]}),
    (
        "Vanilla MLP 6 LoRA",
        "MLP",
        LoraConfig,
        {
            "target_modules": ["lin0"],
            "lora_alpha": 4,
            "lora_dropout": 0.1,
        },
    ),
    ("Vanilla MLP 7 LoRA with DoRA", "MLP", LoraConfig, {"target_modules": ["lin0"], "use_dora": True}),
    ("Vanilla MLP 8 LoRA with DoRA", "MLP", LoraConfig, {"target_modules": ["lin0", "lin1"], "use_dora": True}),
    (
        "Vanilla MLP 9 LoRA with DoRA",
        "MLP",
        LoraConfig,
        {"target_modules": "lin1", "use_dora": True, "lora_alpha": 32},
    ),
    ("Embedding + transformers Conv1D 1 LoRA", "EmbConv1D", LoraConfig, {"target_modules": ["conv1d"]}),
    ("Embedding + transformers Conv1D 2 LoRA", "EmbConv1D", LoraConfig, {"target_modules": ["emb"]}),
    ("Embedding + transformers Conv1D 3 LoRA", "EmbConv1D", LoraConfig, {"target_modules": ["emb", "conv1d"]}),
    (
        "Embedding + transformers Conv1D 1 DoRA",
        "EmbConv1D",
        LoraConfig,
        {"target_modules": ["conv1d"], "use_dora": True},
    ),
    ("Embedding + transformers Conv1D 2 DoRA", "EmbConv1D", LoraConfig, {"target_modules": ["emb"], "use_dora": True}),
    (
        "Embedding + transformers Conv1D 3 DoRA",
        "EmbConv1D",
        LoraConfig,
        {"target_modules": ["emb", "conv1d"], "use_dora": True},
    ),
    (
        "Embedding + transformers Conv1D 1 LoRA trainable_tokens",
        "EmbConv1D",
        LoraConfig,
        {"target_modules": ["conv1d"], "trainable_token_indices": {"emb": [0, 10]}},
    ),
    ("Conv1d LoRA", "Conv1d", LoraConfig, {"target_modules": ["conv1d"]}),
    ("Conv1d LoRA with DoRA", "Conv1d", LoraConfig, {"target_modules": ["conv1d"], "use_dora": True}),
    ("Conv2d 1 LoRA", "Conv2d", LoraConfig, {"target_modules": ["conv2d"]}),
    ("Conv2d 2 LoRA", "Conv2d", LoraConfig, {"target_modules": ["conv2d", "lin0"]}),
    ("Conv2d 1 LoRA with DoRA", "Conv2d", LoraConfig, {"target_modules": ["conv2d"], "use_dora": True}),
    ("Conv2d 2 LoRA with DoRA", "Conv2d", LoraConfig, {"target_modules": ["conv2d", "lin0"], "use_dora": True}),
    ("Conv2d Groups LoRA", "Conv2dGroups", LoraConfig, {"target_modules": ["conv2d"]}),
    ("Conv2d Groups2 LoRA", "Conv2dGroups2", LoraConfig, {"target_modules": ["conv2d"]}),
    ("Conv2d Groups LoRA with DoRA", "Conv2dGroups", LoraConfig, {"target_modules": ["conv2d"], "use_dora": True}),
    ("Conv2d Groups2 LoRA with DoRA", "Conv2dGroups2", LoraConfig, {"target_modules": ["conv2d"], "use_dora": True}),
    ("Conv3d 1 LoRA", "Conv3d", LoraConfig, {"target_modules": ["conv3d"]}),
    ("Conv3d 2 LoRA", "Conv3d", LoraConfig, {"target_modules": ["conv3d", "lin0"]}),
    ("Conv3d 1 LoRA with DoRA", "Conv3d", LoraConfig, {"target_modules": ["conv3d"], "use_dora": True}),
    ("Conv3d 2 LoRA with DoRA", "Conv3d", LoraConfig, {"target_modules": ["conv3d", "lin0"], "use_dora": True}),
    # LoRA with lora_B bias enabled (note: embedding is not supported)
    # It's important to set lora_alpha != r to ensure that scaling is taken into account correctly
    (
        "Vanilla MLP 1 LoRA with lora_b bias",
        "MLP",
        LoraConfig,
        {"target_modules": ["lin0", "lin1"], "lora_bias": True, "lora_alpha": 32},
    ),
    (
        "Conv2d 1 LoRA with lora_b bias",
        "Conv2d",
        LoraConfig,
        {"target_modules": ["conv2d"], "lora_bias": True, "lora_alpha": 32},
    ),
    (
        "Conv3d 1 LoRA with lora_b bias",
        "Conv3d",
        LoraConfig,
        {"target_modules": ["conv3d"], "lora_bias": True, "lora_alpha": 32},
    ),
    ("MHA 1 LoRA", "MHA", LoraConfig, {"target_modules": ["mha"]}),
    ("MHA 2 LoRA", "MHA", LoraConfig, {"target_modules": ["mha", "lin0"]}),
    # targeting parameters directly
    ("MLP 1 using nn.Parameter LoRA", "MlpUsingParameters", LoraConfig, {"target_parameters": ["lin0.weight"]}),
    (
        "MLP 2 using nn.Parameter LoRA",
        "MLP",
        LoraConfig,
        {"target_modules": ["lin0"], "target_parameters": ["lin1.weight"]},
    ),
    #######
    # IA³ #
    #######
    ("Vanilla MLP 1 IA3", "MLP", IA3Config, {"target_modules": "lin0", "feedforward_modules": []}),
    ("Vanilla MLP 2 IA3", "MLP", IA3Config, {"target_modules": "lin0", "feedforward_modules": "lin0"}),
    ("Vanilla MLP 3 IA3", "MLP", IA3Config, {"target_modules": ["lin0"], "feedforward_modules": []}),
    ("Vanilla MLP 4 IA3", "MLP", IA3Config, {"target_modules": ["lin0"], "feedforward_modules": ["lin0"]}),
    ("Vanilla MLP 5 IA3", "MLP", IA3Config, {"target_modules": ["lin1"], "feedforward_modules": []}),
    ("Vanilla MLP 6 IA3", "MLP", IA3Config, {"target_modules": ["lin1"], "feedforward_modules": ["lin1"]}),
    (
        "Vanilla MLP 7 IA3",
        "MLP",
        IA3Config,
        {"target_modules": ["lin0", "lin1"], "feedforward_modules": []},
    ),
    (
        "Vanilla MLP 8 IA3",
        "MLP",
        IA3Config,
        {"target_modules": ["lin0", "lin1"], "feedforward_modules": ["lin0", "lin1"]},
    ),
    (
        "Vanilla MLP 9 IA3",
        "MLP",
        IA3Config,
        {"target_modules": ["lin0"], "modules_to_save": ["lin1"], "feedforward_modules": ["lin0"]},
    ),
    (
        "transformers Conv1D 1 IA3",
        "EmbConv1D",
        IA3Config,
        {"target_modules": ["conv1d"], "feedforward_modules": ["conv1d"]},
    ),
    (
        "transformers Conv1D 2 IA3",
        "EmbConv1D",
        IA3Config,
        {"target_modules": ["conv1d", "lin0"], "feedforward_modules": ["conv1d", "lin0"]},
    ),
    (
        "transformers Conv1D 1 IA3",
        "EmbConv1D",
        IA3Config,
        {"target_modules": ["conv1d"], "feedforward_modules": ["conv1d"], "modules_to_save": ["lin0"]},
    ),
    ("Conv2d 1 IA3", "Conv2d", IA3Config, {"target_modules": ["conv2d"], "feedforward_modules": []}),
    ("Conv2d 2 IA3", "Conv2d", IA3Config, {"target_modules": ["conv2d"], "feedforward_modules": ["conv2d"]}),
    (
        "Conv2d 3 IA3",
        "Conv2d",
        IA3Config,
        {"target_modules": ["conv2d", "lin0"], "feedforward_modules": []},
    ),
    (
        "Conv2d 4 IA3",
        "Conv2d",
        IA3Config,
        {"target_modules": ["conv2d", "lin0"], "feedforward_modules": ["conv2d"]},
    ),
    (
        "Conv2d 5 IA3",
        "Conv2d",
        IA3Config,
        {"target_modules": ["conv2d", "lin0"], "feedforward_modules": ["conv2d", "lin0"]},
    ),
    ("Conv3d 1 IA3", "Conv3d", IA3Config, {"target_modules": ["conv3d"], "feedforward_modules": []}),
    ("Conv3d 2 IA3", "Conv3d", IA3Config, {"target_modules": ["conv3d"], "feedforward_modules": ["conv3d"]}),
    (
        "Conv3d 3 IA3",
        "Conv3d",
        IA3Config,
        {"target_modules": ["conv3d", "lin0"], "feedforward_modules": []},
    ),
    (
        "Conv3d 4 IA3",
        "Conv3d",
        IA3Config,
        {"target_modules": ["conv3d", "lin0"], "feedforward_modules": ["conv3d"]},
    ),
    (
        "Conv3d 5 IA3",
        "Conv3d",
        IA3Config,
        {"target_modules": ["conv3d", "lin0"], "feedforward_modules": ["conv3d", "lin0"]},
    ),
    ########
    # LoHa #
    ########
    ("Vanilla MLP 1 LOHA", "MLP", LoHaConfig, {"target_modules": "lin0"}),
    ("Vanilla MLP 2 LOHA", "MLP", LoHaConfig, {"target_modules": ["lin0"]}),
    ("Vanilla MLP 3 LOHA", "MLP", LoHaConfig, {"target_modules": ["lin1"]}),
    ("Vanilla MLP 4 LOHA", "MLP", LoHaConfig, {"target_modules": ["lin0", "lin1"]}),
    ("Vanilla MLP 5 LOHA", "MLP", LoHaConfig, {"target_modules": ["lin0"], "modules_to_save": ["lin1"]}),
    (
        "Vanilla MLP 6 LOHA",
        "MLP",
        LoHaConfig,
        {
            "target_modules": ["lin0"],
            "alpha": 4,
            "module_dropout": 0.1,
        },
    ),
    ("Vanilla MLP 7 LOHA", "MLP", LoHaConfig, {"target_modules": "lin0", "rank_dropout": 0.5}),
    ("Conv2d 1 LOHA", "Conv2d", LoHaConfig, {"target_modules": ["conv2d"]}),
    ("Conv2d 2 LOHA", "Conv2d", LoHaConfig, {"target_modules": ["conv2d", "lin0"]}),
    ("Conv2d 3 LOHA", "Conv2d", LoHaConfig, {"target_modules": ["conv2d"], "use_effective_conv2d": True}),
    ("Conv2d 4 LOHA", "Conv2d", LoHaConfig, {"target_modules": ["conv2d", "lin0"], "use_effective_conv2d": True}),
    # LoKr
    ("Vanilla MLP 1 LOKR", "MLP", LoKrConfig, {"target_modules": "lin0"}),
    ("Vanilla MLP 2 LOKR", "MLP", LoKrConfig, {"target_modules": ["lin0"]}),
    ("Vanilla MLP 3 LOKR", "MLP", LoKrConfig, {"target_modules": ["lin1"]}),
    ("Vanilla MLP 4 LOKR", "MLP", LoKrConfig, {"target_modules": ["lin0", "lin1"]}),
    ("Vanilla MLP 5 LOKR", "MLP", LoKrConfig, {"target_modules": ["lin0"], "modules_to_save": ["lin1"]}),
    (
        "Vanilla MLP 6 LOKR",
        "MLP",
        LoKrConfig,
        {
            "target_modules": ["lin0"],
            "alpha": 4,
            "module_dropout": 0.1,
        },
    ),
    ("Vanilla MLP 7 LOKR", "MLP", LoKrConfig, {"target_modules": "lin0", "rank_dropout": 0.5}),
    ("Vanilla MLP 8 LOKR", "MLP", LoKrConfig, {"target_modules": "lin0", "decompose_both": True, "r": 1, "alpha": 1}),
    ("Conv2d 1 LOKR", "Conv2d", LoKrConfig, {"target_modules": ["conv2d"]}),
    ("Conv2d 2 LOKR", "Conv2d", LoKrConfig, {"target_modules": ["conv2d", "lin0"]}),
    ("Conv2d 3 LOKR", "Conv2d", LoKrConfig, {"target_modules": ["conv2d"], "use_effective_conv2d": True}),
    ("Conv2d 4 LOKR", "Conv2d", LoKrConfig, {"target_modules": ["conv2d", "lin0"], "use_effective_conv2d": True}),
    (
        "Conv2d 5 LOKR",
        "Conv2d",
        LoKrConfig,
        {"target_modules": ["conv2d", "lin0"], "use_effective_conv2d": True, "decompose_both": True},
    ),
    (
        "Conv2d 6 LOKR",
        "Conv2d",
        LoKrConfig,
        {"target_modules": ["conv2d", "lin0"], "use_effective_conv2d": True, "decompose_factor": 4},
    ),
    (
        "Conv2d 7 LOKR",
        "Conv2d",
        LoKrConfig,
        {
            "target_modules": ["conv2d", "lin0"],
            "use_effective_conv2d": True,
            "decompose_both": True,
            "decompose_factor": 4,
        },
    ),
    ########
    # OFT #
    ########
    (
        "Vanilla MLP 1 OFT",
        "MLP",
        OFTConfig,
        {"r": 2, "oft_block_size": 0, "target_modules": "lin0", "use_cayley_neumann": False},
    ),
    (
        "Vanilla MLP 2 OFT",
        "MLP",
        OFTConfig,
        {"r": 2, "oft_block_size": 0, "target_modules": ["lin0"], "use_cayley_neumann": False},
    ),
    (
        "Vanilla MLP 5 OFT",
        "MLP",
        OFTConfig,
        {
            "r": 2,
            "oft_block_size": 0,
            "target_modules": ["lin0"],
            "modules_to_save": ["lin1"],
            "use_cayley_neumann": False,
        },
    ),
    (
        "Vanilla MLP 6 OFT",
        "MLP",
        OFTConfig,
        {
            "r": 2,
            "oft_block_size": 0,
            "target_modules": ["lin0"],
            "module_dropout": 0.1,
            "use_cayley_neumann": False,
        },
    ),
    (
        "Vanilla MLP 7 OFT",
        "MLP",
        OFTConfig,
        {"r": 2, "oft_block_size": 0, "target_modules": ["lin0"], "coft": True, "eps": 1e-2},
    ),
    (
        "Vanilla MLP 8 OFT",
        "MLP",
        OFTConfig,
        {"r": 2, "oft_block_size": 0, "target_modules": ["lin0"], "block_share": True, "use_cayley_neumann": False},
    ),
    (
        "Vanilla MLP 9 OFT",
        "MLP",
        OFTConfig,
        {"r": 2, "oft_block_size": 0, "target_modules": ["lin0"], "coft": True, "eps": 1e-2, "block_share": True},
    ),
    (
        "Vanilla MLP 10 OFT",
        "MLP",
        OFTConfig,
        {"r": 0, "oft_block_size": 2, "target_modules": ["lin0"], "use_cayley_neumann": True},
    ),
    (
        "Vanilla MLP 11 OFT",
        "MLP",
        OFTConfig,
        {"r": 0, "oft_block_size": 2, "target_modules": ["lin0"], "use_cayley_neumann": False},
    ),
    (
        "Vanilla MLP 12 OFT",
        "MLP",
        OFTConfig,
        {
            "r": 0,
            "oft_block_size": 2,
            "target_modules": ["lin0"],
            "coft": True,
            "eps": 1e-2,
            "block_share": True,
            "use_cayley_neumann": True,
        },
    ),
    (
        "Vanilla MLP 13 OFT",
        "MLP",
        OFTConfig,
        {
            "r": 0,
            "oft_block_size": 2,
            "target_modules": ["lin0"],
            "coft": True,
            "eps": 1e-2,
            "block_share": True,
            "use_cayley_neumann": False,
        },
    ),
    ("Conv2d 1 OFT", "Conv2d", OFTConfig, {"r": 5, "oft_block_size": 0, "target_modules": ["conv2d"]}),
    ("Conv2d 3 OFT", "Conv2d", OFTConfig, {"r": 5, "oft_block_size": 0, "target_modules": ["conv2d"], "coft": True}),
    (
        "Conv2d 4 OFT",
        "Conv2d",
        OFTConfig,
        {"r": 5, "oft_block_size": 0, "target_modules": ["conv2d"], "block_share": True},
    ),
    (
        "Conv2d 5 OFT",
        "Conv2d",
        OFTConfig,
        {"r": 5, "oft_block_size": 0, "target_modules": ["conv2d"], "coft": True, "block_share": True},
    ),
    ########
    # HRA #
    ########
    ("Vanilla MLP 1 HRA", "MLP", HRAConfig, {"target_modules": "lin0"}),
    ("Vanilla MLP 2 HRA", "MLP", HRAConfig, {"target_modules": ["lin0"]}),
    ("Vanilla MLP 3 HRA", "MLP", HRAConfig, {"target_modules": ["lin0", "lin1"]}),
    ("Vanilla MLP 5 HRA", "MLP", HRAConfig, {"target_modules": ["lin0"], "modules_to_save": ["lin1"]}),
    ("Conv2d 1 HRA", "Conv2d", HRAConfig, {"target_modules": ["conv2d"]}),
    ########
    # Bone #
    ########
    ("Vanilla MLP 1 Bone", "MLP", BoneConfig, {"target_modules": "lin0", "r": 2}),
    ("Vanilla MLP 2 Bone", "MLP", BoneConfig, {"target_modules": ["lin0"], "r": 2}),
    ("Vanilla MLP 3 Bone", "MLP", BoneConfig, {"target_modules": ["lin0", "lin1"], "r": 2}),
    ("Vanilla MLP 5 Bone", "MLP", BoneConfig, {"target_modules": ["lin0"], "modules_to_save": ["lin1"], "r": 2}),
    ("Vanilla MLP 1 Bone", "MLP", BoneConfig, {"target_modules": "lin0", "r": 2, "init_weights": "bat"}),
    ("Vanilla MLP 2 Bone", "MLP", BoneConfig, {"target_modules": ["lin0"], "r": 2, "init_weights": "bat"}),
    ("Vanilla MLP 3 Bone", "MLP", BoneConfig, {"target_modules": ["lin0", "lin1"], "r": 2, "init_weights": "bat"}),
    (
        "Vanilla MLP 5 Bone",
        "MLP",
        BoneConfig,
        {"target_modules": ["lin0"], "modules_to_save": ["lin1"], "r": 2, "init_weights": "bat"},
    ),
    ########
    # MiSS #
    ########
    ("Vanilla MLP 1 MiSS", "MLP", MissConfig, {"target_modules": "lin0", "r": 2}),
    ("Vanilla MLP 2 MiSS", "MLP", MissConfig, {"target_modules": ["lin0"], "r": 2}),
    ("Vanilla MLP 3 MiSS", "MLP", MissConfig, {"target_modules": ["lin0", "lin1"], "r": 2}),
    ("Vanilla MLP 5 MiSS", "MLP", MissConfig, {"target_modules": ["lin0"], "modules_to_save": ["lin1"], "r": 2}),
    ("Vanilla MLP 1 MiSS", "MLP", MissConfig, {"target_modules": "lin0", "r": 2, "init_weights": "bat"}),
    ("Vanilla MLP 2 MiSS", "MLP", MissConfig, {"target_modules": ["lin0"], "r": 2, "init_weights": "bat"}),
    ("Vanilla MLP 3 MiSS", "MLP", MissConfig, {"target_modules": ["lin0", "lin1"], "r": 2, "init_weights": "bat"}),
    (
        "Vanilla MLP 5 MiSS",
        "MLP",
        MissConfig,
        {"target_modules": ["lin0"], "modules_to_save": ["lin1"], "r": 2, "init_weights": "bat"},
    ),
    #############
    # LN Tuning #
    #############
    ("LayerNorm 1 LNTuning", "MLP_LayerNorm", LNTuningConfig, {"target_modules": "layernorm0"}),
    ("LayerNorm 2 LNTuning", "MLP_LayerNorm", LNTuningConfig, {"target_modules": ["layernorm0"]}),
    (
        "LayerNorm 3 LNTuning",
        "MLP_LayerNorm",
        LNTuningConfig,
        {"target_modules": ["layernorm0"], "modules_to_save": ["layernorm1"]},
    ),
    ("Linear 4 LNTuning", "MLP_LayerNorm", LNTuningConfig, {"target_modules": "lin0"}),
    ("Linear 5 LNTuning", "MLP_LayerNorm", LNTuningConfig, {"target_modules": ["lin0"]}),
    ########
    # BOFT #
    ########
    ("Vanilla MLP 1 BOFT", "MLP", BOFTConfig, {"target_modules": ["lin1"], "boft_block_size": 2}),
    (
        "Vanilla MLP 2 BOFT",
        "MLP",
        BOFTConfig,
        {"target_modules": ["lin1"], "modules_to_save": ["lin0"], "boft_block_size": 2},
    ),
    (
        "Vanilla MLP 3 BOFT",
        "MLP",
        BOFTConfig,
        {
            "target_modules": ["lin1"],
            "boft_block_size": 2,
            "boft_dropout": 0.1,
        },
    ),
    (
        "Vanilla MLP 4 BOFT",
        "MLP",
        BOFTConfig,
        {"target_modules": ["lin1"], "boft_block_size": 2, "boft_block_num": 0, "boft_n_butterfly_factor": 1},
    ),
    (
        "Vanilla MLP 5 BOFT",
        "MLP",
        BOFTConfig,
        {"target_modules": ["lin1"], "boft_block_size": 0, "boft_block_num": 2, "boft_n_butterfly_factor": 1},
    ),
    (
        "Vanilla MLP 6 BOFT",
        "MLP",
        BOFTConfig,
        {"target_modules": ["lin1"], "boft_block_size": 10, "boft_block_num": 0, "boft_n_butterfly_factor": 2},
    ),
    (
        "Conv2d 1 BOFT",
        "Conv2d",
        BOFTConfig,
        {"target_modules": ["conv2d"], "boft_block_size": 45, "boft_block_num": 0, "boft_n_butterfly_factor": 1},
    ),
    (
        "Conv2d 2 BOFT",
        "Conv2d",
        BOFTConfig,
        {"target_modules": ["conv2d"], "boft_block_size": 0, "boft_block_num": 1, "boft_n_butterfly_factor": 1},
    ),
    (
        "MLP2 1 BOFT",
        "MLP2",
        BOFTConfig,
        {"target_modules": ["lin1"], "boft_block_size": 2, "boft_block_num": 0, "boft_n_butterfly_factor": 3},
    ),
    (
        "MLP2 2 BOFT",
        "MLP2",
        BOFTConfig,
        {"target_modules": ["lin1"], "boft_block_size": 0, "boft_block_num": 8, "boft_n_butterfly_factor": 3},
    ),
    (
        "Conv2d2 1 BOFT",
        "Conv2d2",
        BOFTConfig,
        {"target_modules": ["conv2d"], "boft_block_size": 2, "boft_block_num": 0, "boft_n_butterfly_factor": 2},
    ),
    (
        "Conv2d2 1 BOFT",
        "Conv2d2",
        BOFTConfig,
        {"target_modules": ["conv2d"], "boft_block_size": 2, "boft_block_num": 0, "boft_n_butterfly_factor": 3},
    ),
    #########
    # SHiRA #
    #########
    ("Vanilla MLP 1 SHiRA", "MLP", ShiraConfig, {"r": 1, "target_modules": "lin0", "init_weights": False}),
    ("Vanilla MLP 2 SHiRA", "MLP", ShiraConfig, {"r": 1, "target_modules": ["lin0"], "init_weights": False}),
    ("Vanilla MLP 3 SHiRA", "MLP", ShiraConfig, {"r": 1, "target_modules": ["lin1"], "init_weights": False}),
    (
        "Vanilla MLP 4 SHiRA",
        "MLP",
        ShiraConfig,
        {"r": 1, "target_modules": ["lin0", "lin1"], "random_seed": 56, "init_weights": False},
    ),
    (
        "Vanilla MLP 5 SHiRA",
        "MLP",
        ShiraConfig,
        {"r": 1, "target_modules": ["lin0"], "init_weights": False},
    ),
    ########
    # VeRA #
    ########
    ("Vanilla MLP 1 VeRA", "MLP", VeraConfig, {"target_modules": "lin0"}),
    ("Vanilla MLP 2 VeRA", "MLP", VeraConfig, {"target_modules": ["lin0"]}),
    ("Vanilla MLP 3 VeRA", "MLP", VeraConfig, {"target_modules": ["lin1"]}),
    ("Vanilla MLP 4 VeRA", "MLP", VeraConfig, {"target_modules": ["lin0", "lin1"]}),
    (
        "Vanilla MLP 5 VeRA",
        "MLP",
        VeraConfig,
        {"target_modules": ["lin0"], "modules_to_save": ["lin1"]},
    ),
    (
        "Embedding + transformers Conv1D 1 VeRA",
        "EmbConv1D",
        VeraConfig,
        {"target_modules": ["conv1d"]},
    ),
    ########
    # FourierFT #
    ########
    ("Vanilla MLP 1 FourierFT", "MLP", FourierFTConfig, {"n_frequency": 10, "target_modules": "lin0"}),
    ("Vanilla MLP 2 FourierFT", "MLP", FourierFTConfig, {"n_frequency": 10, "target_modules": ["lin0"]}),
    ("Vanilla MLP 3 FourierFT", "MLP", FourierFTConfig, {"n_frequency": 10, "target_modules": ["lin1"]}),
    (
        "Vanilla MLP 5 FourierFT",
        "MLP",
        FourierFTConfig,
        {"n_frequency": 10, "target_modules": ["lin0"], "modules_to_save": ["lin1"]},
    ),
    (
        "Vanilla MLP 6 FourierFT",
        "MLP",
        FourierFTConfig,
        {"n_frequency": 10, "target_modules": ["lin0", "lin1"], "modules_to_save": ["lin1"]},
    ),
    (
        "Vanilla MLP 7 FourierFT",
        "MLP",
        FourierFTConfig,
        {
            "n_frequency_pattern": {"lin0": 5, "lin1": 10},
            "target_modules": ["lin0", "lin1"],
            "modules_to_save": ["lin1"],
        },
    ),
    ##########
    # VBLoRA #
    ##########
    ("Vanilla MLP 1 VBLoRA", "MLP", VBLoRAConfig, {"target_modules": "lin0", "vector_length": 1, "num_vectors": 5}),
    ("Vanilla MLP 2 VBLoRA", "MLP", VBLoRAConfig, {"target_modules": ["lin0"], "vector_length": 1, "num_vectors": 5}),
    ("Vanilla MLP 3 VBLoRA", "MLP", VBLoRAConfig, {"target_modules": ["lin1"], "vector_length": 2, "num_vectors": 5}),
    (
        "Vanilla MLP 4 VBLoRA",
        "MLP",
        VBLoRAConfig,
        {"target_modules": ["lin0", "lin1"], "vector_length": 1, "num_vectors": 5},
    ),
    (
        "Vanilla MLP 5 VBLoRA",
        "MLP",
        VBLoRAConfig,
        {"target_modules": ["lin0"], "modules_to_save": ["lin1"], "vector_length": 1, "num_vectors": 5},
    ),
    (
        "Embedding + transformers Conv1D 1 VBLoRA",
        "EmbConv1D",
        VBLoRAConfig,
        {"target_modules": ["conv1d"], "vector_length": 1, "num_vectors": 2},
    ),
    ###################
    # TrainableTokens #
    ###################
    (
        "Embedding + transformers Conv1D 1 trainable_tokens",
        "EmbConv1D",
        TrainableTokensConfig,
        {"target_modules": ["emb"], "token_indices": [0, 1, 3], "init_weights": False},
    ),
    ############
    # RandLora #
    ############
    # We have to reduce the default scaling parameter to avoid nans when using large learning rates
    ("Vanilla MLP 1 RandLora", "MLP", RandLoraConfig, {"target_modules": "lin0", "randlora_alpha": 1}),
    ("Vanilla MLP 2 RandLora", "MLP", RandLoraConfig, {"target_modules": ["lin0"], "randlora_alpha": 1}),
    ("Vanilla MLP 3 RandLora", "MLP", RandLoraConfig, {"target_modules": ["lin1"], "randlora_alpha": 1}),
    ("Vanilla MLP 4 RandLora", "MLP", RandLoraConfig, {"target_modules": ["lin0", "lin1"], "randlora_alpha": 1}),
    (
        "Vanilla MLP 5 RandLora",
        "MLP",
        RandLoraConfig,
        {"target_modules": ["lin0", "lin1"], "sparse": True, "randlora_alpha": 1},
    ),
    (
        "Vanilla MLP 6 RandLora",
        "MLP",
        RandLoraConfig,
        {"target_modules": ["lin0", "lin1"], "very_sparse": True, "randlora_alpha": 1},
    ),
    (
        "Vanilla MLP 7 RandLora",
        "MLP",
        RandLoraConfig,
        {"target_modules": ["lin0"], "modules_to_save": ["lin1"], "randlora_alpha": 1},
    ),
    #######
    # C3A #
    #######
    ("Vanilla MLP 1 C3A", "MLP", C3AConfig, {"block_size": 2, "target_modules": "lin0"}),
    ("Vanilla MLP 2 C3A", "MLP", C3AConfig, {"block_size": 2, "target_modules": ["lin0"]}),
    ("Vanilla MLP 3 C3A", "MLP", C3AConfig, {"block_size": 2, "target_modules": ["lin1"]}),
    (
        "Vanilla MLP 5 C3A",
        "MLP",
        C3AConfig,
        {"block_size": 10, "target_modules": ["lin0"], "modules_to_save": ["lin1"]},
    ),
    (
        "Vanilla MLP 6 C3A",
        "MLP",
        C3AConfig,
        {"block_size": 10, "target_modules": ["lin0", "lin1"], "modules_to_save": ["lin1"]},
    ),
    (
        "Vanilla MLP 7 C3A",
        "MLP",
        C3AConfig,
        {
            "block_size_pattern": {"lin0": 5, "lin1": 10},
            "target_modules": ["lin0", "lin1"],
            "modules_to_save": ["lin1"],
        },
    ),
]

# For this test matrix, each tuple consists of:
# - test name
# - tuner method
# - config_cls
# - 1st config kwargs
# - 2nd config kwargs
# The model used for this test is `MLP`, which uses linear layers `lin0` and `lin1`
MULTIPLE_ACTIVE_ADAPTERS_TEST_CASES = [
    (
        "LoRA Same",
        "lora",
        LoraConfig,
        {"target_modules": ["lin0"], "init_lora_weights": False},
        {"target_modules": ["lin0"], "init_lora_weights": False},
    ),
    (
        "LoRA Different",
        "lora",
        LoraConfig,
        {"target_modules": ["lin0"], "init_lora_weights": False},
        {"target_modules": ["lin1"], "init_lora_weights": False},
    ),
    (
        "LoRA + trainable tokens Same",
        "lora+trainable_tokens",
        LoraConfig,
        {"target_modules": ["lin0"], "init_lora_weights": False, "trainable_token_indices": {"emb": [0, 1, 2]}},
        {"target_modules": ["lin0"], "init_lora_weights": False, "trainable_token_indices": {"emb": [3, 4, 5, 6]}},
    ),
    (
        "LoRA + trainable tokens Different",
        "lora+trainable_tokens",
        LoraConfig,
        {"target_modules": ["lin0"], "init_lora_weights": False, "trainable_token_indices": {"emb": [0, 1, 2]}},
        {"target_modules": ["lin1"], "init_lora_weights": False, "trainable_token_indices": {"emb": [3, 4, 5, 6]}},
    ),
    (
        "LoRA targeting nn.Parameter Same",
        "lora",
        LoraConfig,
        {"target_parameters": ["lin0.weight"], "init_lora_weights": False},
        {"target_parameters": ["lin0.weight"], "init_lora_weights": False},
    ),
    (
        "LoRA targeting nn.Parameter Different",
        "lora",
        LoraConfig,
        {"target_parameters": ["lin0.weight"], "init_lora_weights": False},
        {"target_parameters": ["lin1.weight"], "init_lora_weights": False},
    ),
    (
        "IA3 Same",
        "ia3",
        IA3Config,
        {
            "target_modules": ["lin0"],
            "feedforward_modules": ["lin0"],
            "init_ia3_weights": False,
        },
        {
            "target_modules": ["lin0"],
            "feedforward_modules": ["lin0"],
            "init_ia3_weights": False,
        },
    ),
    (
        "IA3 Different",
        "ia3",
        IA3Config,
        {
            "target_modules": ["lin0"],
            "feedforward_modules": ["lin0"],
            "init_ia3_weights": False,
        },
        {
            "target_modules": ["lin1"],
            "feedforward_modules": ["lin1"],
            "init_ia3_weights": False,
        },
    ),
    (
        "AdaLora Same",
        "adalora",
        AdaLoraConfig,
        {"target_modules": ["lin0"], "init_lora_weights": False, "inference_mode": True, "total_step": 1},
        {"target_modules": ["lin0"], "init_lora_weights": False, "inference_mode": True, "total_step": 1},
    ),
    (
        "AdaLora Different",
        "adalora",
        AdaLoraConfig,
        {"target_modules": ["lin0"], "init_lora_weights": False, "inference_mode": True, "total_step": 1},
        {"target_modules": ["lin1"], "init_lora_weights": False, "inference_mode": True, "total_step": 1},
    ),
    (
        "FourierFT Same",
        "fourierft",
        FourierFTConfig,
        {"n_frequency": 10, "target_modules": ["lin0"]},
        {"n_frequency": 10, "target_modules": ["lin0"]},
    ),
    (
        "FourierFT Different",
        "fourierft",
        FourierFTConfig,
        {"n_frequency": 10, "target_modules": ["lin0"]},
        {"n_frequency": 10, "target_modules": ["lin1"]},
    ),
    (
        "SHiRA Same",
        "shira",
        ShiraConfig,
        {"r": 1, "target_modules": ["lin0"], "init_weights": False},
        {"r": 1, "target_modules": ["lin0"], "init_weights": False},
    ),
    (
        "SHiRA Different",
        "shira",
        ShiraConfig,
        {"r": 1, "target_modules": ["lin0"], "init_weights": False},
        {"r": 1, "target_modules": ["lin1"], "init_weights": False},
    ),
    # Note: Currently, we cannot target lin0 and lin1 with different adapters when using VeRA. The reason is that the
    # first adapter being created will result in a vera_A or vera_B shape that is too small for the next adapter
    # (remember that VeRA shares these parameters across all layers), which results in an error.
    (
        "VeRA Same",
        "vera",
        VeraConfig,
        {"target_modules": ["lin0"], "init_weights": False},
        {"target_modules": ["lin0"], "init_weights": False},
    ),
    # Note: RandLora may present the same problem mentioned above for Vera.
    (
        "RandLora Same",
        "randlora",
        RandLoraConfig,
        {"target_modules": ["lin0"], "init_weights": False},
        {"target_modules": ["lin0"], "init_weights": False},
    ),
    (
        "HRA Same",
        "hra",
        HRAConfig,
        {"target_modules": ["lin0"], "init_weights": False},
        {"target_modules": ["lin0"], "init_weights": False},
    ),
    (
        "HRA Different",
        "hra",
        HRAConfig,
        {"target_modules": ["lin0"], "init_weights": False},
        {"target_modules": ["lin1"], "init_weights": False},
    ),
    (
        "Bone Same",
        "bone",
        BoneConfig,
        {"target_modules": ["lin0"], "init_weights": False, "r": 2},
        {"target_modules": ["lin0"], "init_weights": False, "r": 2},
    ),
    (
        "Bone Different",
        "bone",
        BoneConfig,
        {"target_modules": ["lin0"], "init_weights": False, "r": 2},
        {"target_modules": ["lin1"], "init_weights": False, "r": 2},
    ),
    (
        "MiSS Same",
        "miss",
        MissConfig,
        {"target_modules": ["lin0"], "init_weights": False, "r": 2},
        {"target_modules": ["lin0"], "init_weights": False, "r": 2},
    ),
    (
        "MiSS Different",
        "miss",
        MissConfig,
        {"target_modules": ["lin0"], "init_weights": False, "r": 2},
        {"target_modules": ["lin1"], "init_weights": False, "r": 2},
    ),
    # Not testing "mini" initialization targeting the same layer, because The matrix is initialized to all zeros in MiSS-mini mode.
    (
        "VBLoRA Same",
        "vblora",
        VBLoRAConfig,
        {"target_modules": ["lin0"], "vector_length": 2, "init_vector_bank_bound": 0.1},
        {"target_modules": ["lin0"], "vector_length": 2, "init_vector_bank_bound": 0.1},
    ),
    (
        "VBLoRA Different",
        "vblora",
        VBLoRAConfig,
        {"target_modules": ["lin0"], "vector_length": 2, "init_vector_bank_bound": 0.1},
        {"target_modules": ["lin1"], "vector_length": 2, "init_vector_bank_bound": 0.1},
    ),
    (
        "BOFT Same",
        "boft",
        BOFTConfig,
        {"target_modules": ["lin0"], "init_weights": False, "boft_block_size": 2},
        {"target_modules": ["lin0"], "init_weights": False, "boft_block_size": 2},
    ),
    (
        "BOFT Different",
        "boft",
        BOFTConfig,
        {"target_modules": ["lin0"], "init_weights": False, "boft_block_size": 2},
        {"target_modules": ["lin1"], "init_weights": False, "boft_block_size": 2},
    ),
]

PREFIXES = {
    IA3Config: "ia3_",
    LoraConfig: "lora_",
    LoHaConfig: "hada_",
    LoKrConfig: "lokr_",
    OFTConfig: "oft_",
    BOFTConfig: "boft_",
    LNTuningConfig: "ln_tuning_",
    VeraConfig: "vera_lambda_",
    RandLoraConfig: "randlora_",
    FourierFTConfig: "fourierft_",
    C3AConfig: "c3a_",
    HRAConfig: "hra_",
    ShiraConfig: "shira_",
    VBLoRAConfig: "vblora_",
    BoneConfig: "bone_",
    MissConfig: "miss_",
    TrainableTokensConfig: "trainable_tokens_",
}


class MLP(nn.Module):
    def __init__(self, bias=True):
        super().__init__()
        self.lin0 = nn.Linear(10, 20, bias=bias)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.5)
        self.lin1 = nn.Linear(20, 2, bias=bias)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = self.lin0(X)
        X = self.relu(X)
        X = self.drop(X)
        X = self.lin1(X)
        X = self.sm(X)
        return X


class MLPWithGRU(nn.Module):
    def __init__(self, bias=True):
        super().__init__()
        self.lin0 = nn.Linear(10, 20, bias=bias)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.5)
        self.gru = nn.GRU(input_size=20, hidden_size=20, num_layers=1, batch_first=True, bias=bias)
        self.fc = nn.Linear(20, 2, bias=bias)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = self.lin0(X)
        X = self.relu(X)
        X = self.drop(X)
        X = X.unsqueeze(1)
        X, _ = self.gru(X)
        X = X.squeeze(1)
        X = self.fc(X)
        X = self.sm(X)
        return X


class MLP_LayerNorm(nn.Module):
    def __init__(self, bias=True):
        super().__init__()
        self.layernorm0 = nn.LayerNorm(10, 10)
        self.lin0 = nn.Linear(10, 20, bias=bias)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.5)
        self.layernorm1 = nn.LayerNorm(20, 20)
        self.lin1 = nn.Linear(20, 2, bias=bias)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = self.layernorm0(X)
        X = self.lin0(X)
        X = self.relu(X)
        X = self.drop(X)
        X = self.layernorm1(X)
        X = self.lin1(X)
        X = self.sm(X)
        return X


class MLP2(nn.Module):
    def __init__(self, bias=True):
        super().__init__()
        self.lin0 = nn.Linear(10, 32, bias=bias)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.5)
        self.lin1 = nn.Linear(32, 2, bias=bias)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = self.lin0(X)
        X = self.relu(X)
        X = self.drop(X)
        X = self.lin1(X)
        X = self.sm(X)
        return X


class Block(nn.Module):
    def __init__(self, bias=True):
        super().__init__()
        self.lin0 = nn.Linear(10, 20, bias=bias)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.5)
        self.lin1 = nn.Linear(20, 10, bias=bias)

    def forward(self, X):
        X = X.float()
        X = self.lin0(X)
        X = self.relu(X)
        X = self.drop(X)
        X = self.lin1(X)
        return X


class DeepMLP(nn.Module):
    def __init__(self, bias=True, num_hidden_layers=12):
        super().__init__()
        self.layers = nn.ModuleList([Block(bias=bias) for _ in range(num_hidden_layers)])
        self.out = nn.Linear(10, 2, bias=bias)
        self.sm = nn.LogSoftmax(dim=-1)

    def forward(self, X):
        X = X.float(X)
        for layer in self.layers:
            X = layer(X)
        X = self.out(X)
        X = self.sm(X)
        return X


class ModelEmbConv1D(nn.Module):
    def __init__(self, emb_size=100):
        super().__init__()
        self.emb = nn.Embedding(emb_size, 5)
        self.conv1d = Conv1D(1, 5)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin0 = nn.Linear(10, 2)
        self.sm = nn.LogSoftmax(dim=-1)

    def forward(self, X):
        X = self.emb(X)
        X = self.conv1d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin0(X)
        X = self.sm(X)
        return X


class ModelEmbWithEmbeddingUtils(nn.Module):
    # Adds `get_input_embeddings` and `get_output_embeddings` methods to mimic 🤗 transformers models
    def __init__(self):
        super().__init__()
        self.embed_tokens = nn.Embedding(100, 5)
        self.conv1d = Conv1D(1, 5)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin0 = nn.Linear(10, 2)
        self.sm = nn.LogSoftmax(dim=-1)

    def forward(self, X):
        X = self.embed_tokens(X)
        X = self.conv1d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin0(X)
        X = self.sm(X)
        return X

    def get_input_embeddings(self):
        return self.embed_tokens

    def get_output_embeddings(self):
        return None


class ModelConv1D(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1d = nn.Conv1d(1, 1, 2)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin0 = nn.Linear(9, 2)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = X.reshape(-1, 1, 10)
        X = self.conv1d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin0(X)
        X = self.sm(X)
        return X


class ModelConv2D(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv2d = nn.Conv2d(5, 10, 3)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin0 = nn.Linear(10, 2)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = X.reshape(-1, 5, 3, 3)
        X = self.conv2d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin0(X)
        X = self.sm(X)
        return X


class ModelConv2D2(nn.Module):
    def __init__(self):
        super().__init__()
        self.lin0 = nn.Linear(10, 40)
        self.conv2d = nn.Conv2d(8, 32, 3)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin1 = nn.Linear(32, 2)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = self.lin0(X)
        X = self.relu(X)
        X = X.reshape(-1, 8, 3, 3)
        X = self.conv2d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin1(X)
        X = self.sm(X)
        return X


class ModelConv2DGroups(nn.Module):
    def __init__(self):
        super().__init__()
        self.lin0 = nn.Linear(90, 288)
        # groups is set as 8 since default r=8
        # hence to make r divisible by groups
        self.conv2d = nn.Conv2d(16, 16, 3, groups=8)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin1 = nn.Linear(16, 2)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = X.flatten()
        X = self.lin0(X)
        X = X.reshape(2, 16, 3, 3)
        X = self.conv2d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin1(X)
        X = self.sm(X)
        return X


class ModelConv2DGroups2(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv2d = nn.Conv2d(16, 32, 3, padding=1, groups=2)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin0 = nn.Linear(12800, 2)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        # Note: needs a different input shape, thus ignore original input
        X = torch.arange(9 * 16 * 20 * 20).view([9, 16, 20, 20]).to(self.conv2d.weight.device)
        X = X.to(self.dtype)
        X = self.conv2d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin0(X)
        X = self.sm(X)
        return X


class ModelConv3D(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv3d = nn.Conv3d(5, 10, 3)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin0 = nn.Linear(10, 2)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        # If necessary, convert from 2D image to 3D volume
        if X.dim() == 2:
            X = torch.stack([X] * 3, dim=-1)
        X = X.reshape(-1, 5, 3, 3, 3)
        X = self.conv3d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin0(X)
        X = self.sm(X)
        return X


class ModelMha(nn.Module):
    def __init__(self):
        super().__init__()
        self.mha = nn.MultiheadAttention(10, 2)
        self.lin0 = nn.Linear(10, 2)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X, _ = self.mha(X, X, X)
        X = self.lin0(X)
        X = self.sm(X)
        return X


class _LinearUsingParameter(nn.Module):
    # Linear layer equivalent
    def __init__(self, in_features, out_features, bias=None):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.weight = nn.Parameter(torch.randn(in_features, out_features))
        if bias:
            self.bias = nn.Parameter(torch.ones(out_features))

    def forward(self, x):
        return x @ self.weight + self.bias


class MlpUsingParameters(nn.Module):
    # MLP that uses layers whose parameters need to be targeted with target_parameters
    def __init__(self, bias=True):
        super().__init__()

        self.lin0 = _LinearUsingParameter(10, 20, bias=bias)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.5)
        self.lin1 = _LinearUsingParameter(20, 2, bias=bias)
        self.sm = nn.LogSoftmax(dim=-1)
        self.dtype = torch.float

    def forward(self, X):
        X = X.to(self.dtype)
        X = self.lin0(X)
        X = self.relu(X)
        X = self.drop(X)
        X = self.lin1(X)
        X = self.sm(X)
        return X


class MockTransformerWrapper:
    """Mock class to behave like a transformers model.

    This is needed because the tests initialize the model by calling transformers_class.from_pretrained.

    """

    @classmethod
    def from_pretrained(cls, model_id, torch_dtype=None):
        # set the seed so that from_pretrained always returns the same model
        torch.manual_seed(0)

        if torch_dtype is None:
            torch_dtype = torch.float32

        if model_id == "MLP":
            return MLP().to(torch_dtype)

        if model_id == "EmbConv1D":
            return ModelEmbConv1D().to(torch_dtype)

        if model_id == "Conv1d":
            return ModelConv1D().to(torch_dtype)

        if model_id == "Conv2d":
            return ModelConv2D().to(torch_dtype)

        if model_id == "Conv2dGroups":
            return ModelConv2DGroups().to(torch_dtype)

        if model_id == "Conv2dGroups2":
            return ModelConv2DGroups2().to(torch_dtype)

        if model_id == "Conv3d":
            return ModelConv3D().to(torch_dtype)

        if model_id == "MLP_LayerNorm":
            return MLP_LayerNorm().to(torch_dtype)

        if model_id == "MLP2":
            return MLP2().to(torch_dtype)

        if model_id == "Conv2d2":
            return ModelConv2D2().to(torch_dtype)

        if model_id == "MHA":
            return ModelMha().to(torch_dtype)

        if model_id == "MlpUsingParameters":
            return MlpUsingParameters().to(torch_dtype)

        raise ValueError(f"model_id {model_id} not implemented")


class TestPeftCustomModel(PeftCommonTester):
    """
    Implements the tests for custom models.

    Most tests should just call the parent class, e.g. test_save_pretrained calls self._test_save_pretrained. Override
    this if custom models don't work with the parent test method.

    """

    transformers_class = MockTransformerWrapper

    def prepare_inputs_for_testing(self):
        X = torch.arange(90).view(9, 10).to(self.torch_device)
        return {"X": X}

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_attributes_parametrized(self, test_name, model_id, config_cls, config_kwargs):
        self._test_model_attr(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_adapter_name(self, test_name, model_id, config_cls, config_kwargs):
        self._test_adapter_name(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_prepare_for_training_parametrized(self, test_name, model_id, config_cls, config_kwargs):
        # This test does not work with custom models because it assumes that
        # there is always a method get_input_embeddings that returns a layer
        # which does not need updates. Instead, a new test is added below that
        # checks that LoRA works as expected.
        pass

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_save_pretrained(self, test_name, model_id, config_cls, config_kwargs):
        self._test_save_pretrained(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_save_pretrained_pickle(self, test_name, model_id, config_cls, config_kwargs):
        self._test_save_pretrained(model_id, config_cls, config_kwargs, safe_serialization=False)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_load_model_low_cpu_mem_usage(self, test_name, model_id, config_cls, config_kwargs):
        self._test_load_model_low_cpu_mem_usage(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_from_pretrained_config_construction(self, test_name, model_id, config_cls, config_kwargs):
        self._test_from_pretrained_config_construction(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_load_multiple_adapters(self, test_name, model_id, config_cls, config_kwargs):
        self._test_load_multiple_adapters(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_merge_layers(self, test_name, model_id, config_cls, config_kwargs):
        # https://github.com/huggingface/peft/pull/2403
        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            pytest.skip(
                f"Skipping test for {model_id} as merging is not supported. (See https://github.com/huggingface/peft/pull/2403 for details)"
            )

        config_kwargs = config_kwargs.copy()
        if issubclass(config_cls, LoraConfig):
            config_kwargs["init_lora_weights"] = False
        elif issubclass(config_cls, IA3Config):
            config_kwargs["init_ia3_weights"] = False
        elif issubclass(config_cls, LNTuningConfig):
            pass
        elif issubclass(config_cls, VBLoRAConfig):
            pass
        elif issubclass(config_cls, TrainableTokensConfig):
            pass
        else:
            config_kwargs["init_weights"] = False
        self._test_merge_layers(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_merge_layers_fp16(self, test_name, model_id, config_cls, config_kwargs):
        # https://github.com/huggingface/peft/pull/2403
        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            pytest.skip(
                f"Skipping test for {model_id} as merging is not supported. (See https://github.com/huggingface/peft/pull/2403 for details)"
            )

        config_kwargs = config_kwargs.copy()
        if issubclass(config_cls, LoraConfig):
            config_kwargs["init_lora_weights"] = False
        elif issubclass(config_cls, IA3Config):
            config_kwargs["init_ia3_weights"] = False
        self._test_merge_layers_fp16(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_merge_layers_is_idempotent(self, test_name, model_id, config_cls, config_kwargs):
        # https://github.com/huggingface/peft/pull/2403
        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            pytest.skip(
                f"Skipping test for {model_id} as merging is not supported. (See https://github.com/huggingface/peft/pull/2403 for details)"
            )

        # calling merge twice with the same arguments should not change the output
        config_kwargs = config_kwargs.copy()
        if issubclass(config_cls, LoraConfig):
            config_kwargs["init_lora_weights"] = False
        elif issubclass(config_cls, IA3Config):
            config_kwargs["init_ia3_weights"] = False
        self._test_merge_layers_is_idempotent(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_safe_merge(self, test_name, model_id, config_cls, config_kwargs):
        # https://github.com/huggingface/peft/pull/2403
        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            pytest.skip(
                f"Skipping test for {model_id} as merging is not supported. (See https://github.com/huggingface/peft/pull/2403 for details)"
            )

        # calling merge twice with the same arguments should not change the output
        config_kwargs = config_kwargs.copy()
        if issubclass(config_cls, LoraConfig):
            config_kwargs["init_lora_weights"] = False
        elif issubclass(config_cls, IA3Config):
            config_kwargs["init_ia3_weights"] = False
        elif issubclass(config_cls, LNTuningConfig):
            # LNTuning do not take init_weights
            pass
        elif issubclass(config_cls, VBLoRAConfig):
            # VBLoRA do not take init_weights
            pass
        else:
            config_kwargs["init_weights"] = False
        self._test_safe_merge(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_generate(self, test_name, model_id, config_cls, config_kwargs):
        # Custom models do not (necessarily) have a generate method, so this test is not performed
        pass

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_generate_half_prec(self, test_name, model_id, config_cls, config_kwargs):
        # Custom models do not (necessarily) have a generate method, so this test is not performed
        pass

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_training_custom_models(self, test_name, model_id, config_cls, config_kwargs):
        self._test_training(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_training_custom_models_layer_indexing(self, test_name, model_id, config_cls, config_kwargs):
        # At the moment, layer indexing only works when layer names conform to a specific pattern, which is not
        # guaranteed here. Therefore, this test is not performed.
        pass

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_training_custom_models_gradient_checkpointing(self, test_name, model_id, config_cls, config_kwargs):
        self._test_training_gradient_checkpointing(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_inference_safetensors(self, test_name, model_id, config_cls, config_kwargs):
        self._test_inference_safetensors(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_peft_model_device_map(self, test_name, model_id, config_cls, config_kwargs):
        self._test_peft_model_device_map(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_forward_output_finite(self, test_name, model_id, config_cls, config_kwargs):
        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        model.eval()
        with torch.no_grad():
            output = model(**X)
        assert torch.isfinite(output).all()

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_forward_float16(self, test_name, model_id, config_cls, config_kwargs):
        # The user manually sets the dtype of the base model to fp16 precision. This should not cause an error for the
        # different PEFT methods.
        try:
            torch.zeros(1, dtype=torch.float16)
        except Exception:
            # skip this test if float16 is not supported on this machine
            pytest.skip(reason="Test requires float16 support")

        # skip on MacOS
        if platform.system() == "Darwin":
            pytest.skip(reason="MacOS does not support multiple ops in float16")

        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id, torch_dtype=torch.float16).to(self.torch_device)
        model.dtype = torch.float16
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        model.eval()

        # check that none of this raises an error
        model(**X)

        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            # this model does not support merging
            return

        model.merge_adapter(safe_merge=False)
        model(**X)
        model.unmerge_adapter()
        model(**X)
        model.merge_adapter(safe_merge=True)
        model(**X)
        model.unmerge_adapter()
        model(**X)
        model = model.merge_and_unload()
        model(**X)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_forward_bfloat16(self, test_name, model_id, config_cls, config_kwargs):
        # The user manually sets the dtype of the base model to bf16 precision. This should not cause an error for the
        # different PEFT methods.
        try:
            torch.zeros(1, dtype=torch.bfloat16)
        except Exception:
            # skip this test if float16 is not supported on this machine
            pytest.skip(reason="Test requires bfloat16 support")

        # skip on MacOS
        if platform.system() == "Darwin":
            pytest.skip(reason="MacOS does not support multiple ops in bfloat16")

        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id, torch_dtype=torch.bfloat16).to(self.torch_device)
        model.dtype = torch.bfloat16
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        model.eval()

        # check that none of this raises an error
        model(**X)

        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            # this model does not support merging
            return

        model.merge_adapter(safe_merge=False)
        model(**X)
        model.unmerge_adapter()
        model(**X)
        model.merge_adapter(safe_merge=True)
        model(**X)
        model.unmerge_adapter()
        model(**X)
        model = model.merge_and_unload()
        model(**X)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_forward_float16_no_autocast(self, test_name, model_id, config_cls, config_kwargs):
        # Same as above but don't autocast adapter weights to float32 automatically
        try:
            torch.zeros(1, dtype=torch.float16)
        except Exception:
            # skip this test if float16 is not supported on this machine
            pytest.skip(reason="Test requires float16 support")

        # skip on MacOS
        if platform.system() == "Darwin":
            pytest.skip(reason="MacOS does not support multiple ops in float16")

        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id, torch_dtype=torch.float16).to(self.torch_device)
        model.dtype = torch.float16
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config, autocast_adapter_dtype=False)
        model.eval()

        # check that none of this raises an error
        model(**X)

        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            # this model does not support merging
            return

        model.merge_adapter(safe_merge=False)
        model(**X)
        model.unmerge_adapter()
        model(**X)
        model.merge_adapter(safe_merge=True)
        model(**X)
        model.unmerge_adapter()
        model(**X)
        model = model.merge_and_unload()
        model(**X)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_forward_bfloat16_no_autocast(self, test_name, model_id, config_cls, config_kwargs):
        # Same as above but don't autocast adapter weights to float32 automatically
        try:
            torch.zeros(1, dtype=torch.bfloat16)
        except Exception:
            # skip this test if float16 is not supported on this machine
            pytest.skip(reason="Test requires bfloat16 support")

        # skip on MacOS
        if platform.system() == "Darwin":
            pytest.skip(reason="MacOS does not support multiple ops in bfloat16")

        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id, torch_dtype=torch.bfloat16).to(self.torch_device)
        model.dtype = torch.bfloat16
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config, autocast_adapter_dtype=False)
        model.eval()

        # check that none of this raises an error
        model(**X)

        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            # this model does not support merging
            return

        model.merge_adapter(safe_merge=False)
        model(**X)
        model.unmerge_adapter()
        model(**X)
        model.merge_adapter(safe_merge=True)
        model(**X)
        model.unmerge_adapter()
        model(**X)
        model = model.merge_and_unload()
        model(**X)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_only_params_are_updated(self, test_name, model_id, config_cls, config_kwargs):
        # An explicit test that when using an adapter on a custom model, only the adapter parameters are updated during
        # training
        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        model_before = copy.deepcopy(model)

        model.train()
        lr = 0.5
        if (config_kwargs.get("use_dora") and model_id == "EmbConv1D") or issubclass(config_cls, VBLoRAConfig):
            # this high learning rate was found through testing to be necessary to avoid flakiness
            lr = 100
        elif "mha" in model_id.lower():
            # we get exploding gradients with MHA when learning rate is too high
            lr = 1e-3
        optimizer = torch.optim.SGD(model.parameters(), lr=lr)

        # train at least 3 steps for all parameters to be updated (probably this is required because of symmetry
        # breaking of some LoRA layers that are initialized with constants)
        for _ in range(3):
            optimizer.zero_grad()
            y_pred = model(**X)
            loss = y_pred.sum()
            loss.backward()
            optimizer.step()

        tol = 1e-4
        params_before = dict(model_before.named_parameters())
        params_after = dict(model.named_parameters())
        assert params_before.keys() == params_after.keys()

        prefix = PREFIXES[config_cls]
        for name, param_before in params_before.items():
            param_after = params_after[name]
            if (prefix in name) or ("modules_to_save" in name) or ("token_adapter.trainable_tokens" in name):
                # target_modules, modules_to_save and modules of `NewTokensWrapper` _are_ updated
                assert not torch.allclose(param_before, param_after, atol=tol, rtol=tol)
            else:
                assert torch.allclose(param_before, param_after, atol=tol, rtol=tol)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_parameters_after_loading_model(self, test_name, model_id, config_cls, config_kwargs):
        # An explicit test that when loading a trained model, the parameters are loaded correctly
        # see issue #808
        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        model.train()

        lr = 0.5
        if config_kwargs.get("use_dora"):
            lr = 0.1  # otherwise we get nan
        elif "mha" in model_id.lower():
            lr = 1e-3  # we get exploding gradients with MHA when learning rate is too high
        elif issubclass(config_cls, VBLoRAConfig) or issubclass(config_cls, RandLoraConfig):
            lr = 0.01  # otherwise we get nan
        optimizer = torch.optim.SGD(model.parameters(), lr=lr)

        # train at least 3 steps for all parameters to be updated (probably this is required because of symmetry
        # breaking of some LoRA layers that are initialized with constants)
        for _ in range(3):
            optimizer.zero_grad()
            y_pred = model(**X)
            loss = y_pred.sum()
            loss.backward()
            optimizer.step()

        tol = 1e-4
        params_before = get_state_dict(model)
        # note: no need to sanity check if parameters were updated at all, this
        # is already covered in the previous test

        with tempfile.TemporaryDirectory() as tmp_dirname:
            model.save_pretrained(tmp_dirname)
            model_from_pretrained = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
            model_from_pretrained = PeftModel.from_pretrained(model_from_pretrained, tmp_dirname)
            params_after = get_state_dict(model_from_pretrained)

            assert params_before.keys() == params_after.keys()
            for name, param_before in params_before.items():
                param_after = params_after[name]
                assert torch.allclose(param_before, param_after, atol=tol, rtol=tol)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_disable_adapters(self, test_name, model_id, config_cls, config_kwargs):
        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device).eval()
        outputs_base = model(**X)
        if issubclass(config_cls, (FourierFTConfig, TrainableTokensConfig, C3AConfig)):
            config_kwargs = config_kwargs.copy()
            # override the default value and make PEFT operation a no-op
            config_kwargs["init_weights"] = True
        if issubclass(config_cls, (ShiraConfig,)):
            # for SHiRA, setting this to default value of True will turn the PEFT operation into a no-op
            # because SHiRA is always initialized to zeros. Configs declared in the test file had set init_weights
            # to False (to make sure all other tests have a randn SHiRA initialization). Setting it back to True here
            # as required by this test.
            config_kwargs["init_weights"] = True
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        if issubclass(config_cls, VBLoRAConfig):
            # Manually set the `vblora_vector_bank` to zero so that VB-LoRA functions as an identity operation.
            torch.nn.init.zeros_(model.vblora_vector_bank["default"])
        model.eval()
        outputs_before = model(**X)
        assert torch.allclose(outputs_base, outputs_before)

        if issubclass(config_cls, VBLoRAConfig):
            # initialize `vblora_vector_bank` so it can be trained
            model._init_vblora_vector_bank(config, "default")
        model.train()
        # EmbConv1D is slow to learn for some reason
        lr = 0.01 if model_id != "EmbConv1D" else 1.0
        if isinstance(config, TrainableTokensConfig):
            # TrainableTokens is only changing a small subset, so we need a higher lr to see the difference
            lr = 2.0
        optimizer = torch.optim.SGD(model.parameters(), lr=lr)

        # train at least 3 steps for all parameters to be updated (probably this is required because of symmetry
        # breaking of some LoRA layers that are initialized with constants)
        for _ in range(3):
            optimizer.zero_grad()
            y_pred = model(**X)
            y = torch.arange(len(y_pred)).to(self.torch_device) % 2
            loss = nn.functional.nll_loss(y_pred, y)
            loss.backward()
            optimizer.step()

        model.eval()
        outputs_after = model(**X)

        with model.disable_adapter():
            outputs_disabled = model(**X)

        # check that after leaving the disable_adapter context, everything is enabled again
        outputs_enabled_after_disable = model(**X)

        if self.torch_device == "cpu":
            # LayerNorm is running float32 on cpu, so difference in outputs are smaller
            rtol, atol = 1e-8, 1e-8
        else:
            rtol, atol = 1e-5, 1e-8
        assert not torch.allclose(outputs_before, outputs_after, rtol=rtol, atol=atol)
        assert torch.allclose(outputs_before, outputs_disabled)
        assert torch.allclose(outputs_after, outputs_enabled_after_disable)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_disable_adapters_with_merging(self, test_name, model_id, config_cls, config_kwargs):
        # https://github.com/huggingface/peft/pull/2403
        if model_id in ["Conv2dGroups", "Conv2dGroups2"]:
            pytest.skip(
                f"Skipping test for {model_id} as merging is not supported. (See https://github.com/huggingface/peft/pull/2403 for details)"
            )

        # same as test_disable_adapters, but with merging
        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        if issubclass(config_cls, (FourierFTConfig, C3AConfig)):
            config_kwargs = config_kwargs.copy()
            config_kwargs["init_weights"] = True
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        if issubclass(config_cls, VBLoRAConfig):
            # Manually set the `vblora_vector_bank` to zero so that VB-LoRA functions as an identity operation.
            torch.nn.init.zeros_(model.vblora_vector_bank["default"])
        model.eval()
        outputs_before = model(**X)

        if issubclass(config_cls, VBLoRAConfig):
            # initialize `vblora_vector_bank` so it can be trained
            model._init_vblora_vector_bank(config, "default")
        model.train()
        if isinstance(config_cls, LNTuningConfig):
            # LayerNorm tuning is slow to learn
            lr = 1.0
            optimizer = torch.optim.SGD(model.parameters(), lr=lr)
        else:
            # Adam optimizer since SGD isn't great for small models with IA3 + Conv1D
            lr = 0.01
            optimizer = torch.optim.Adam(model.parameters(), lr=lr)

        # train at least 3 steps for all parameters to be updated (probably this is required because of symmetry
        # breaking of some LoRA layers that are initialized with constants)
        for _ in range(3):
            optimizer.zero_grad()
            y_pred = model(**X)
            y = torch.arange(len(y_pred)).to(self.torch_device) % 2
            loss = nn.functional.nll_loss(y_pred, y)
            loss.backward()
            optimizer.step()

        model.eval()
        outputs_unmerged = model(**X)
        model.merge_adapter()
        outputs_after = model(**X)

        with model.disable_adapter():
            outputs_disabled = model(**X)

        # check that after leaving the disable_adapter context, everything is enabled again
        outputs_enabled_after_disable = model(**X)

        atol, rtol = 1e-5, 1e-5  # tolerances higher than defaults since merging introduces some numerical instability

        conv_ids = ["Conv2d", "Conv3d", "Conv2d2"]
        if issubclass(config_cls, (IA3Config, LoraConfig)) and model_id in conv_ids:  # more instability with Conv
            atol, rtol = 1e-3, 1e-3

        if issubclass(config_cls, OFTConfig):
            atol, rtol = 1e-4, 1e-4

        if config_kwargs.get("use_dora") and model_id == "EmbConv1D":
            atol, rtol = 1e-4, 1e-4

        # check that there is a difference in results after training
        assert not torch.allclose(outputs_before, outputs_after, atol=atol, rtol=rtol)

        if self.torch_device in ["mlu"] and model_id in conv_ids:
            atol, rtol = 1e-3, 1e-2  # MLU

        # unmerged or merged should make no difference
        assert torch.allclose(outputs_after, outputs_unmerged, atol=atol, rtol=rtol)

        # check that disabling adapters gives the same results as before training
        assert torch.allclose(outputs_before, outputs_disabled, atol=atol, rtol=rtol)

        # check that enabling + disabling adapters does not change the results
        assert torch.allclose(outputs_after, outputs_enabled_after_disable, atol=atol, rtol=rtol)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_disable_adapter_with_bias_warns(self, test_name, model_id, config_cls, config_kwargs):
        # When training biases in lora, disabling adapters does not reset the biases, so the output is not what users
        # might expect. Therefore, a warning should be given.

        # Note: We test only with custom models since they run really fast. There is really no point in testing the same
        # thing with decoder, encoder_decoder, etc.
        if config_cls != LoraConfig or config_cls != BOFTConfig:
            # skip this test for other configs as bias is specific to Lora
            pytest.skip("Testing bias warnings only for LoraConfig or BOFTConfig")

        if not issubclass(config_cls, (LoraConfig, BOFTConfig)):
            pytest.skip("Bias argument is only supported for LoRA or BOFT models")

        def run_with_disable(config_kwargs, bias):
            config_kwargs = config_kwargs.copy()
            config_kwargs["bias"] = bias
            model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
            config = config_cls(
                base_model_name_or_path=model_id,
                **config_kwargs,
            )
            peft_model = get_peft_model(model, config)
            with peft_model.disable_adapter():
                pass  # there is nothing to be done

        if config_cls == LoraConfig:
            # check that bias=all and bias=lora_only give a warning with the correct message
            msg_start = "Careful, disabling adapter layers with bias configured to be"
            with pytest.warns(UserWarning, match=msg_start):
                run_with_disable(config_kwargs, bias="lora_only")
            with pytest.warns(UserWarning, match=msg_start):
                run_with_disable(config_kwargs, bias="all")

        if config_cls == BOFTConfig:
            # check that bias=all and bias=boft_only give a warning with the correct message
            msg_start = "Careful, disabling adapter layers with bias configured to be"
            with pytest.warns(UserWarning, match=msg_start):
                run_with_disable(config_kwargs, bias="boft_only")
            with pytest.warns(UserWarning, match=msg_start):
                run_with_disable(config_kwargs, bias="all")

        # For bias=none, there is no warning. Unfortunately, AFAIK unittest has no option to assert that no warning is
        # given, therefore, we check that the unittest gives us an AssertionError if we check for a warning
        bias_warning_was_given = False
        try:
            with pytest.warns(UserWarning) as cm:
                run_with_disable(config_kwargs, bias="none")
                # if we get here, it means there was no AssertionError, i.e. there are warnings -- let's check that they
                # are not related to the bias setting
                if any(warning.message.args[0].startswith(msg_start) for warning in cm.warnings):
                    bias_warning_was_given = True
        except AssertionError:
            # This is good, there was an AssertionError, i.e. there was no warning
            pass
        if bias_warning_was_given:
            # This is bad, there was a warning about the bias when there should not have been any.
            self.fail("There should be no warning when bias is set to 'none'")

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_active_adapter(self, test_name, model_id, config_cls, config_kwargs):
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        assert model.active_adapters == ["default"]
        assert model.active_adapter == "default"

        # at this stage, "default" is still the activate adapter, "other" is disabled
        model.add_adapter("other", config)
        assert model.active_adapters == ["default"]
        assert model.active_adapter == "default"

        # set "other" as the active adapter
        model.set_adapter("other")
        assert model.active_adapters == ["other"]
        assert model.active_adapter == "other"

        # set both adapters as active
        # Note: On the PeftModel, there cannot be multiple active adapters, so we have to go through model.base_model
        # instead.
        model.base_model.set_adapter(["default", "other"])
        # model.active_adapters works, as it delegates to the base_model
        assert model.active_adapters == ["default", "other"]
        # model.active_adapter would not work, thus we have to check the base_model directly
        assert model.base_model.active_adapter == ["default", "other"]

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_disable_adapters_exiting_context_restores_previous_state(
        self, test_name, model_id, config_cls, config_kwargs
    ):
        # Test that when we exit the disable_adapter context, we correctly restore the enabled state of the modules as
        # they were before the context.
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        tuner_modules = [module for module in model.modules() if isinstance(module, BaseTunerLayer)]

        # all layers should be enabled
        assert all(not module.disable_adapters for module in tuner_modules)
        with model.disable_adapter():
            pass
        # this should not change after exiting the context
        assert all(not module.disable_adapters for module in tuner_modules)

        # now disable all layers
        model.disable_adapter_layers()
        assert all(module.disable_adapters for module in tuner_modules)
        with model.disable_adapter():
            pass
        assert all(module.disable_adapters for module in tuner_modules)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_disable_adapters_exiting_context_irregular_state(self, test_name, model_id, config_cls, config_kwargs):
        # When we have a model where some adapters are enabled and others are disabled, we should get a warning when
        # entering the disable_adapter context because we cannot correctly restore the state of the adapters from
        # before the context. After exiting the context, all adapters will be enabled, which is the status quo of how
        # we deal with this.
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        tuner_modules = [module for module in model.modules() if isinstance(module, BaseTunerLayer)]

        # now we mix the states, some enabled some not
        if len(tuner_modules) < 2:
            # next check only works with more than 1 tuner module
            return

        # disable a single layer
        tuner_modules[0].enable_adapters(False)
        # sanity check that we have both enabled and disabled layers
        assert {module.disable_adapters for module in tuner_modules} == {True, False}
        # check that we get a warning with irregular states
        msg = "The model contains some adapter layers that are enabled and others that are disabled"
        with pytest.warns(UserWarning, match=msg):
            with model.disable_adapter():
                pass

        # when encountering irregular adapters, we enable all adapters at the end of the context
        assert all(not module.disable_adapters for module in tuner_modules)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_delete_adapter(self, test_name, model_id, config_cls, config_kwargs):
        self._test_delete_adapter(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_delete_inactive_adapter(self, test_name, model_id, config_cls, config_kwargs):
        self._test_delete_inactive_adapter(model_id, config_cls, config_kwargs)

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_delete_unknown_adapter_raises(self, test_name, model_id, config_cls, config_kwargs):
        self._test_delete_unknown_adapter_raises(model_id, config_cls, config_kwargs)

    def test_delete_adapter_with_multiple_adapters_works(self):
        # Add 3 adapters, delete the active one, the next one should be active, delete the inactive one, the active one
        # should stay the same.
        config0 = LoraConfig(target_modules=["lin0"])
        config1 = LoraConfig(target_modules=["lin0"])
        config2 = LoraConfig(target_modules=["lin0"])
        model = get_peft_model(MLP(), config0, adapter_name="adapter0").to(self.torch_device)
        model.add_adapter("adapter1", config1)
        model.add_adapter("adapter2", config2)

        inputs = self.prepare_inputs_for_testing()
        assert model.active_adapters == ["adapter0"]
        model(**inputs)  # does not raise

        # delete the active adapter, next one should become active
        model.delete_adapter("adapter0")
        assert model.active_adapters == ["adapter1"]
        model(**inputs)  # does not raise

        # delete an inactive adapter, should not affect the active adapter
        model.delete_adapter("adapter2")
        assert model.active_adapters == ["adapter1"]
        model(**inputs)  # does not raise

    def test_delete_adapter_multiple_adapters_with_modules_to_save(self):
        # There are 3 adapters. Adapter 0 has modules_to_save. Delete it, we should switch to adapter 1, which does not
        # have modules_to_save. Then, we delete it too, switching to adapter 2, which has modules_to_save. Finally, we
        # delete the last adapter (state is updated but forward is no longer possible).
        model = MLP()
        inputs = self.prepare_inputs_for_testing()

        config0 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        config1 = LoraConfig(target_modules=["lin0"])
        config2 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        model = get_peft_model(model, config0, adapter_name="adapter0").to(self.torch_device)
        model.add_adapter("adapter1", config1)
        model.add_adapter("adapter2", config2)

        assert model.active_adapters == ["adapter0"]
        assert model.modules_to_save == {"lin1"}
        assert set(model.base_model.model.lin1.modules_to_save) == {"adapter0", "adapter2"}
        model(**inputs)  # does not raise

        # delete active adapter, should switch to the next adapter (which does not have modules_to_save)
        model.delete_adapter("adapter0")
        assert model.active_adapters == ["adapter1"]
        assert model.modules_to_save == {"lin1"}
        assert set(model.base_model.model.lin1.modules_to_save) == {"adapter2"}
        model(**inputs)  # does not raise

        # delete active adapter, should switch to the next adapter (which *does* have modules_to_save)
        model.delete_adapter("adapter1")
        assert model.active_adapters == ["adapter2"]
        assert model.modules_to_save == {"lin1"}
        assert set(model.base_model.model.lin1.modules_to_save) == {"adapter2"}
        model(**inputs)  # does not raise

        # delete last adapter
        model.delete_adapter("adapter2")
        assert model.active_adapters == []
        assert model.modules_to_save is None
        assert set(model.base_model.model.lin1.modules_to_save) == set()

    def test_delete_adapter_multiple_adapters_with_trainable_token_indices(self):
        # Same as the previous test, just using trainable_token_indices instead of modules_to_save
        # Note that we need to use a transformers model for trainable_token_indices
        model = AutoModelForCausalLM.from_pretrained("hf-internal-testing/tiny-random-OPTForCausalLM")
        inputs = {"input_ids": torch.arange(10).view(-1, 1).to(self.torch_device)}

        config0 = LoraConfig(target_modules=["q_proj"], trainable_token_indices=[0, 1])
        config1 = LoraConfig(target_modules=["q_proj"])
        config2 = LoraConfig(target_modules=["q_proj"], trainable_token_indices=[1, 3])
        model = get_peft_model(model, config0, adapter_name="adapter0").to(self.torch_device)
        model.add_adapter("adapter1", config1)
        model.add_adapter("adapter2", config2)

        embed_tokens = model.base_model.model.model.decoder.embed_tokens
        lm_head = model.base_model.model.lm_head

        assert model.active_adapters == ["adapter0"]
        assert set(embed_tokens.token_adapter.trainable_tokens_delta) == {"adapter0", "adapter2"}
        assert set(embed_tokens.token_adapter.trainable_tokens_original) == {"adapter0", "adapter2"}
        assert set(lm_head.token_adapter.trainable_tokens_delta) == {"adapter0", "adapter2"}
        assert set(lm_head.token_adapter.trainable_tokens_original) == {"adapter0", "adapter2"}
        model(**inputs)  # does not raise

        # delete active adapter, should switch to the next adapter (which does not have modules_to_save)
        model.delete_adapter("adapter0")
        assert model.active_adapters == ["adapter1"]
        assert set(embed_tokens.token_adapter.trainable_tokens_delta) == {"adapter2"}
        assert set(embed_tokens.token_adapter.trainable_tokens_original) == {"adapter2"}
        assert set(lm_head.token_adapter.trainable_tokens_delta) == {"adapter2"}
        assert set(lm_head.token_adapter.trainable_tokens_original) == {"adapter2"}
        model(**inputs)  # does not raise

        # delete active adapter, should switch to the next adapter (which *does* have modules_to_save)
        model.delete_adapter("adapter1")
        assert model.active_adapters == ["adapter2"]
        assert set(embed_tokens.token_adapter.trainable_tokens_delta) == {"adapter2"}
        assert set(embed_tokens.token_adapter.trainable_tokens_original) == {"adapter2"}
        assert set(lm_head.token_adapter.trainable_tokens_delta) == {"adapter2"}
        assert set(lm_head.token_adapter.trainable_tokens_original) == {"adapter2"}
        model(**inputs)  # does not raise

        # delete last adapter
        model.delete_adapter("adapter2")
        assert model.active_adapters == []
        assert set(embed_tokens.token_adapter.trainable_tokens_delta) == set()
        assert set(embed_tokens.token_adapter.trainable_tokens_original) == set()
        assert set(lm_head.token_adapter.trainable_tokens_delta) == set()
        assert set(lm_head.token_adapter.trainable_tokens_original) == set()

    @pytest.mark.parametrize("test_name, model_id, config_cls, config_kwargs", TEST_CASES)
    def test_adding_multiple_adapters_with_bias_raises(self, test_name, model_id, config_cls, config_kwargs):
        self._test_adding_multiple_adapters_with_bias_raises(model_id, config_cls, config_kwargs)

    def test_weight_bias_attributes(self):
        model = MLP()
        config = LoraConfig(target_modules=["lin0"])
        model = get_peft_model(model, config)
        assert hasattr(model.base_model.model.lin0, "weight")
        assert hasattr(model.base_model.model.lin0, "bias")

    def test_multiple_adapters_automatic_modules_to_save(self):
        # See issue 1574
        # When we use certain task types, PeftModel.modules_to_save is automatically updated to include some extra
        # layers not specified in the PeftConfig. This attribute should be honored for all adapters, not just for
        # the default adapter.
        config0 = LoraConfig(task_type=TaskType.SEQ_CLS)
        config1 = LoraConfig(task_type=TaskType.SEQ_CLS)
        model = AutoModelForSequenceClassification.from_pretrained("bert-base-uncased")
        model = get_peft_model(model, config0)
        # sanity check
        assert model.modules_to_save

        model.add_adapter("other", config1)
        assert "default" in model.base_model.classifier.modules_to_save
        assert "other" in model.base_model.classifier.modules_to_save

    @pytest.mark.parametrize(
        "config_cls", [IA3Config, LoHaConfig, LoKrConfig, LoraConfig, HRAConfig, BoneConfig, ShiraConfig, MissConfig]
    )
    def test_multiple_adapters_mixed_modules_to_save(self, config_cls):
        # See issue 1574
        # Check that we can have a model where one adapter has modules_to_save and the other doesn't. It should be
        # possible to switch between those adapters and to use them.
        if hasattr(config_cls, "feedforward_modules"):  # IA³
            config_cls = partial(config_cls, feedforward_modules=["lin0"])

        if config_cls == BoneConfig or config_cls == MissConfig:
            config_cls = partial(config_cls, r=2)
        if config_cls == ShiraConfig:
            config_cls = partial(config_cls, r=1)

        config0 = config_cls(target_modules=["lin0"], modules_to_save=["lin1"])
        config1 = config_cls(target_modules=["lin0"])
        model = MLP()
        model = get_peft_model(model, config0).to(self.torch_device)
        model.add_adapter("other", config1)

        assert "default" in model.base_model.lin1.modules_to_save
        assert "other" not in model.base_model.lin1.modules_to_save

        # check that switching adapters and predicting does not raise
        inputs = self.prepare_inputs_for_testing()
        # "default" adapter is active
        model(**inputs)
        # switch to "other" adapter
        model.set_adapter("other")
        model(**inputs)

    @pytest.mark.parametrize(
        "config_cls", [IA3Config, LoHaConfig, LoKrConfig, LoraConfig, HRAConfig, BoneConfig, ShiraConfig]
    )
    def test_multiple_adapters_mixed_modules_to_save_order_switched(self, config_cls):
        # See issue 1574
        # Same test as test_multiple_adapters_mixed_modules_to_save, but this time the 2nd adapter has modules_to_save.
        if hasattr(config_cls, "feedforward_modules"):  # IA³
            config_cls = partial(config_cls, feedforward_modules=["lin0"])

        if config_cls == BoneConfig or config_cls == MissConfig:
            config_cls = partial(config_cls, r=2)
        if config_cls == ShiraConfig:
            config_cls = partial(config_cls, r=1)

        config0 = config_cls(target_modules=["lin0"])
        config1 = config_cls(target_modules=["lin0"], modules_to_save=["lin1"])
        model = MLP()
        model = get_peft_model(model, config0).to(self.torch_device)
        model.add_adapter("other", config1)

        assert "default" not in model.base_model.lin1.modules_to_save
        assert "other" in model.base_model.lin1.modules_to_save

        # check that switching adapters and predicting does not raise
        inputs = self.prepare_inputs_for_testing()
        # "default" adapter is active
        model(**inputs)
        # switch to "other" adapter
        model.set_adapter("other")
        model(**inputs)

    def test_multiple_adapters_mixed_modules_to_save_merging_adapters(self):
        # See issue 1574
        # This test is similar to test_multiple_adapters_mixed_modules_to_save, but it also checks that merging adapter
        # weights works when one adapter has a modules_to_save and the other hasn't
        config0 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        config1 = LoraConfig(target_modules=["lin0"])
        model = MLP()
        model = get_peft_model(model, config0).to(self.torch_device)
        model.add_adapter("other", config1)

        # check that this does not raise
        model.add_weighted_adapter(["default", "other"], weights=[1.0, 1.0], adapter_name="merged")

        # since one of the adapters that was merged has a modules_to_save, that one should be used for the merged
        # adapter
        assert "default" in model.base_model.model.lin1.modules_to_save
        assert "other" not in model.base_model.model.lin1.modules_to_save
        assert "merged" in model.base_model.model.lin1.modules_to_save

        # check that using the merged adapter does not raise
        model.set_adapter("merged")
        inputs = self.prepare_inputs_for_testing()
        model(**inputs)

    def test_multiple_adapters_same_modules_to_save_merging_adapters_raises(self):
        # See issue 1574
        # This test is similar to test_multiple_adapters_mixed_modules_to_save_merging_adapters but here the two
        # adapters target the same module with modules_to_save. In this case, trying to merge the adapter weights
        # should raise an error.
        config0 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        config1 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        model = MLP()
        model = get_peft_model(model, config0).to(self.torch_device)
        model.add_adapter("other", config1)

        msg = re.escape(
            "Cannot add weighted adapters if they target the same module with modules_to_save, but found 1 such "
            "instance(s)."
        )
        with pytest.raises(ValueError, match=msg):
            model.add_weighted_adapter(["default", "other"], weights=[1.0, 1.0], adapter_name="merged")

    def test_multiple_adapters_seq_cls_mixed_modules_to_save_merging_adapters(self):
        # See issue 1574
        # This test is similar to test_multiple_adapters_mixed_modules_to_save_merging_adapters but uses a SEQ_CLS
        # model like in test_multiple_adapters_automatic_modules_to_save. This should raise an error because the same
        # module is implicitly targeted by modules_to_save twice.
        config0 = LoraConfig(task_type=TaskType.SEQ_CLS)
        config1 = LoraConfig(task_type=TaskType.SEQ_CLS)
        model = AutoModelForSequenceClassification.from_pretrained("bert-base-uncased")
        model = get_peft_model(model, config0)
        model.add_adapter("other", config1)

        msg = re.escape(
            "Cannot add weighted adapters if they target the same module with modules_to_save, but found 1 such "
            "instance(s)."
        )
        with pytest.raises(ValueError, match=msg):
            model.add_weighted_adapter(["default", "other"], weights=[1.0, 1.0], adapter_name="merged")

    @pytest.mark.parametrize(
        "config_cls", [IA3Config, LoHaConfig, LoKrConfig, LoraConfig, HRAConfig, BoneConfig, MissConfig]
    )
    def test_add_weighted_adapter_cat_with_rank_pattern(self, config_cls):
        # Fixes a bug described in #2512, which resulted from the rank_pattern not being taken into account
        config0 = LoraConfig(target_modules=["lin0", "lin1"], r=8, rank_pattern={"lin0": 2})
        config1 = LoraConfig(target_modules=["lin0", "lin1"], r=8, rank_pattern={"lin0": 16})
        model = MLP()
        model = get_peft_model(model, config0).to(self.torch_device)
        model.add_adapter("other", config1)
        model.add_weighted_adapter(
            ["default", "other"], weights=[1.0, 1.0], adapter_name="merged", combination_type="cat"
        )

    def test_multiple_adapters_no_needless_copy_modules_to_save(self):
        # See 2206
        # The problem was that we keep a "global" modules_to_save on the model which contains all possible
        # modules_to_save for each adapter. When the first adapter targets embed_tokens with modules_to_save and the
        # second adapter targets lm_head, then embed_tokens will create a copy of the original module for the second
        # adapter, even though it's not needed. The copy still acts as expected but uses unnecessary memory.
        model_id = "hf-internal-testing/tiny-random-OPTForCausalLM"
        model = AutoModelForCausalLM.from_pretrained(model_id).to(self.torch_device)
        config0 = LoraConfig(modules_to_save=["embed_tokens"])
        config1 = LoraConfig(modules_to_save=["lm_head"])
        model = get_peft_model(model, config0)
        model.add_adapter("other", config1)

        lm_head_keys = list(model.base_model.model.lm_head.modules_to_save.keys())
        assert lm_head_keys == ["other"]

        embed_token_keys = list(model.base_model.model.model.decoder.embed_tokens.modules_to_save.keys())
        # before the fix, this would be: ['default', 'other']
        assert embed_token_keys == ["default"]

    def test_existing_model_card(self):
        # ensure that if there is already a model card, it is not overwritten
        model = MLP()
        config = LoraConfig(target_modules=["lin0"])
        model = get_peft_model(model, config)

        with tempfile.TemporaryDirectory() as tmp_dirname:
            # create a model card
            text = "---\nmeta: hello\n---\nThis is a model card\n"
            with open(os.path.join(tmp_dirname, "README.md"), "w") as f:
                f.write(text)

            model.save_pretrained(tmp_dirname)
            with open(os.path.join(tmp_dirname, "README.md")) as f:
                model_card = f.read()

        assert "library_name: peft" in model_card
        assert "meta: hello" in model_card
        assert "This is a model card" in model_card

    def test_non_existing_model_card(self):
        # ensure that if there is already a model card, it is not overwritten
        model = MLP()
        config = LoraConfig(target_modules=["lin0"])
        model = get_peft_model(model, config)

        with tempfile.TemporaryDirectory() as tmp_dirname:
            model.save_pretrained(tmp_dirname)
            with open(os.path.join(tmp_dirname, "README.md")) as f:
                model_card = f.read()

        assert "library_name: peft" in model_card
        # rough check that the model card is pre-filled
        assert len(model_card) > 1000

    @pytest.mark.parametrize("save_embedding_layers", ["auto", True, False])
    @pytest.mark.parametrize(
        "peft_config",
        [
            (LoraConfig(target_modules=["lin0", "embed_tokens"], init_lora_weights=False)),
            (LoraConfig(target_modules=r"^embed_tokens", init_lora_weights=False)),
        ],
    )
    def test_save_pretrained_targeting_lora_to_embedding_layer(self, save_embedding_layers, tmp_path, peft_config):
        model = ModelEmbWithEmbeddingUtils()
        model = get_peft_model(model, peft_config)

        if save_embedding_layers == "auto":
            # assert warning
            msg_start = "Setting `save_embedding_layers` to `True` as embedding layers found in `target_modules`."
            with pytest.warns(UserWarning, match=msg_start):
                model.save_pretrained(tmp_path, save_embedding_layers=save_embedding_layers)
        else:
            model.save_pretrained(tmp_path, save_embedding_layers=save_embedding_layers)

        state_dict = safe_load_file(tmp_path / "adapter_model.safetensors")
        contains_embedding = "base_model.model.embed_tokens.base_layer.weight" in state_dict

        if save_embedding_layers in ["auto", True]:
            assert contains_embedding
            assert torch.allclose(
                model.base_model.model.embed_tokens.base_layer.weight,
                state_dict["base_model.model.embed_tokens.base_layer.weight"],
            )
        else:
            assert not contains_embedding

    @pytest.mark.parametrize("save_embedding_layers", ["auto", True, False])
    @pytest.mark.parametrize(
        "peft_config",
        [
            (LoraConfig(target_modules=["lin0", "emb"], init_lora_weights=False)),
            (LoraConfig(target_modules=r"^emb", init_lora_weights=False)),
        ],
    )
    def test_save_pretrained_targeting_lora_to_embedding_layer_non_transformers(
        self, save_embedding_layers, tmp_path, peft_config
    ):
        model = ModelEmbConv1D()
        model = get_peft_model(model, peft_config)

        if save_embedding_layers is True:
            with pytest.warns(
                UserWarning,
                match=r"Could not identify embedding layer\(s\) because the model is not a 🤗 transformers model\.",
            ):
                model.save_pretrained(tmp_path, save_embedding_layers=save_embedding_layers)
        else:
            model.save_pretrained(tmp_path, save_embedding_layers=save_embedding_layers)

        state_dict = safe_load_file(tmp_path / "adapter_model.safetensors")
        assert "base_model.model.emb.base_layer.weight" not in state_dict

    def test_load_resized_embedding_ignore_mismatched_sizes(self):
        # issue #1605
        # Make it possible to load a LoRA layer that targets an embedding layer even if the sizes mismatch by passing
        # ignore_mismatched_sizes=True
        model = ModelEmbConv1D(emb_size=100)
        config = LoraConfig(target_modules=["emb", "lin0"], init_lora_weights=False)
        model = get_peft_model(model, config)

        # note: not using the context manager here because it fails on Windows CI for some reason
        tmp_dirname = tempfile.mkdtemp()
        try:
            model.save_pretrained(tmp_dirname)
            model = ModelEmbConv1D(emb_size=105)

            # first check that this raises
            with pytest.raises(RuntimeError) as exc:
                PeftModel.from_pretrained(model, tmp_dirname)
            msg = exc.value.args[0]
            assert "size mismatch" in msg and "100" in msg and "105" in msg

            # does not raise
            PeftModel.from_pretrained(model, tmp_dirname, ignore_mismatched_sizes=True)
        finally:
            try:
                shutil.rmtree(tmp_dirname)
            except PermissionError:
                # windows error
                pass

    @pytest.mark.parametrize(
        "config0",
        [
            LoraConfig(target_modules=["lin0"], init_lora_weights=False),
            LoKrConfig(target_modules=["lin0"], init_weights=False),
            LoHaConfig(target_modules=["lin0"], init_weights=False),
            AdaLoraConfig(target_modules=["lin0"], init_lora_weights=False, total_step=1),
            IA3Config(target_modules=["lin0"], feedforward_modules=["lin0"], init_ia3_weights=False),
            OFTConfig(target_modules=["lin0"], init_weights=False, r=2, oft_block_size=0),
            BOFTConfig(target_modules=["lin0"], init_weights=False, boft_block_size=2),
            HRAConfig(target_modules=["lin0"], init_weights=False),
            BoneConfig(target_modules=["lin0"], init_weights=False, r=2),
            MissConfig(target_modules=["lin0"], init_weights=False, r=2),
        ],
    )
    def test_adapter_name_makes_no_difference(self, config0):
        # It should not matter whether we use the default adapter name or a custom one
        model_cls = MLP
        input = torch.arange(90).reshape(9, 10).to(self.torch_device)

        # base model
        torch.manual_seed(0)
        base_model = model_cls().eval().to(self.torch_device)
        output_base = base_model(input)

        # default name
        torch.manual_seed(0)
        base_model = model_cls().eval().to(self.torch_device)
        torch.manual_seed(0)
        peft_model_default = get_peft_model(base_model, config0, adapter_name="default").eval().to(self.torch_device)
        output_default = peft_model_default(input)
        sd_default = peft_model_default.state_dict()

        # custom name 1
        torch.manual_seed(0)
        base_model = model_cls().eval().to(self.torch_device)
        torch.manual_seed(0)
        peft_model_custom1 = get_peft_model(base_model, config0, adapter_name="adapter").eval().to(self.torch_device)
        output_custom1 = peft_model_custom1(input)
        sd_custom1 = peft_model_custom1.state_dict()

        # custom name 2
        torch.manual_seed(0)
        base_model = model_cls().eval().to(self.torch_device)
        torch.manual_seed(0)
        peft_model_custom2 = (
            get_peft_model(base_model, config0, adapter_name="other-name").eval().to(self.torch_device)
        )
        output_custom2 = peft_model_custom2(input)
        sd_custom2 = peft_model_custom2.state_dict()

        assert len(sd_default) == len(sd_custom1) == len(sd_custom2)
        for key in sd_default:
            key1 = key.replace("default", "adapter")
            key2 = key.replace("default", "other-name")
            assert key1 in sd_custom1
            assert key2 in sd_custom2
        for k0, k1, k2 in zip(sd_default, sd_custom1, sd_custom2):
            assert torch.allclose(sd_default[k0], sd_custom1[k1])
            assert torch.allclose(sd_default[k0], sd_custom2[k2])

        assert not torch.allclose(output_base, output_default)
        assert not torch.allclose(output_base, output_custom1)
        assert not torch.allclose(output_base, output_custom2)
        assert torch.allclose(output_custom1, output_custom2)
        assert torch.allclose(output_default, output_custom1)

    def test_gpt2_dora_merge_and_unload(self):
        # see https://github.com/huggingface/peft/pull/1588#discussion_r1537914207
        model = AutoModelForCausalLM.from_pretrained("gpt2")
        config = LoraConfig(task_type="CAUSAL_LM", use_dora=True)
        model = get_peft_model(model, config)
        # should not raise an error
        model.merge_and_unload()

    def test_gpt2_dora_merge_and_unload_safe_merge(self):
        # see https://github.com/huggingface/peft/pull/1588#discussion_r1537914207
        model = AutoModelForCausalLM.from_pretrained("gpt2")
        config = LoraConfig(task_type="CAUSAL_LM", use_dora=True)
        model = get_peft_model(model, config)
        # should not raise an error
        model.merge_and_unload(safe_merge=True)

    def test_unload_adapter_multihead_attention(self):
        # MultiheadAttention has special logic for unloading, that logic is covered by this test
        self._test_unload_adapter(
            model_id="MHA",
            config_cls=LoraConfig,
            config_kwargs={"target_modules": ["mha"], "init_lora_weights": False},
        )

    def test_dora_save_and_load_remapping(self):
        # Here we test the refactor of DoRA which changed lora_magnitude_vector from a ParameterDict to a ModuleDict
        # with a DoraLayer instance. The old parameter is now the "weight" attribute of that layer. Since we want the
        # state_dict format not to change, we ensure that the ".weight" part of the key is removed.
        model = AutoModelForCausalLM.from_pretrained("facebook/opt-125m")
        config = LoraConfig(task_type="CAUSAL_LM", use_dora=True)
        model = get_peft_model(model, config)
        state_dict = model.state_dict()

        # sanity check: state dict contains "lora_magnitude_vector.default.weight" keys
        assert any("lora_magnitude_vector.default.weight" in k for k in state_dict)

        # save the model, check the state dict
        # note: not using the context manager here because it fails on Windows CI for some reason
        tmp_dirname = tempfile.mkdtemp()
        try:
            model.save_pretrained(tmp_dirname)
            state_dict_adapter = safe_load_file(os.path.join(tmp_dirname, "adapter_model.safetensors"))
            # note that in the state dict, the "default" part of the key is removed
            assert not any("lora_magnitude_vector.weight" in k for k in state_dict_adapter)

            del model
            loaded = PeftModel.from_pretrained(AutoModelForCausalLM.from_pretrained("facebook/opt-125m"), tmp_dirname)
        finally:
            try:
                shutil.rmtree(tmp_dirname)
            except PermissionError:
                # windows error
                pass

        state_dict_loaded = loaded.state_dict()
        assert state_dict.keys() == state_dict_loaded.keys()
        for k in state_dict:
            assert torch.allclose(state_dict[k], state_dict_loaded[k])

    @pytest.mark.parametrize("with_forward_call", [False, True])
    def test_mha_gradients_set_correctly(self, with_forward_call):
        # check for this bug: https://github.com/huggingface/peft/issues/761#issuecomment-1893804738
        base_model = ModelMha()
        config = LoraConfig(target_modules=["mha"])
        model = get_peft_model(base_model, config)
        model = model.to(self.torch_device)

        if with_forward_call:
            # after the merge-unmerge roundtrip happening in forward of lora MHA, the base weights should be set to
            # requires_grad=False
            inputs = self.prepare_inputs_for_testing()
            model(**inputs)

        assert model.base_model.model.mha.base_layer.out_proj.base_layer.weight.requires_grad is False
        assert model.base_model.model.mha.base_layer.in_proj_weight.requires_grad is False

        # _restore_weights used to ignore the gradient, this checks that it is indeed considered
        model.base_model.model.mha._restore_weights()
        assert model.base_model.model.mha.base_layer.out_proj.base_layer.weight.requires_grad is False
        assert model.base_model.model.mha.base_layer.in_proj_weight.requires_grad is False

        model.base_model.model.mha.base_layer.out_proj.base_layer.weight.requires_grad = True
        model.base_model.model.mha.base_layer.in_proj_weight.requires_grad = True
        assert model.base_model.model.mha.base_layer.out_proj.base_layer.weight.requires_grad is True
        assert model.base_model.model.mha.base_layer.in_proj_weight.requires_grad is True

        model.base_model.model.mha._restore_weights()
        assert model.base_model.model.mha.base_layer.out_proj.base_layer.weight.requires_grad is True
        assert model.base_model.model.mha.base_layer.in_proj_weight.requires_grad is True


class TestMultiRankAdapter:
    """Tests related to multirank LoRA adapters"""

    def test_multirank(self):
        config_1 = LoraConfig(
            r=8,
            lora_alpha=8,
            init_lora_weights=False,
            target_modules=["lin0", "lin1"],
        )
        config_2 = LoraConfig(
            r=8,
            lora_alpha=8,
            init_lora_weights=False,
            target_modules=["lin0", "lin1"],
            rank_pattern={"lin0": 4},
            alpha_pattern={"lin0": 4},
        )

        # Add first adapter
        model = get_peft_model(MLP(), config_1, adapter_name="first")

        # Add second adapter
        model.add_adapter("second", config_2)

        # Extract current and expected ranks
        rank_current = model.lin0.lora_A["second"].weight.shape[0]
        rank_expected = config_2.rank_pattern["lin0"]

        assert rank_current == rank_expected, f"Rank {rank_current} is not equal to expected {rank_expected}"

    def test_multirank_2(self):
        rank_pattern = {}
        alpha_pattern = {}
        r = 4
        lora_alpha = 8

        for i in range(10):
            rank = 64 // (i + 1)
            for j in range(2):
                rank_pattern[f"layers.{i}.lin{j}"] = rank
                alpha_pattern[f"layers.{i}.lin{j}"] = 2 * rank

        config = LoraConfig(
            r=r,
            lora_alpha=lora_alpha,
            init_lora_weights=False,
            target_modules=["lin0", "lin1"],
            rank_pattern=rank_pattern,
            alpha_pattern=alpha_pattern,
        )

        # Add first adapter
        model = get_peft_model(DeepMLP(), config, adapter_name="first")

        # Add second adapter
        model.add_adapter("second", config)

        for adapter in ["first", "second"]:
            for key, module in model.base_model.model.named_modules():
                if isinstance(module, BaseTunerLayer):
                    rank_expected = rank_pattern.get(key, r)
                    rank_current = module.lora_A[adapter].weight.shape[0]
                    assert rank_current == rank_expected, (
                        f"Rank {rank_current} is not equal to expected {rank_expected}"
                    )


class TestLayerRepr:
    """Tests related to the repr of adapted models"""

    def test_repr_lora_linear(self):
        config = LoraConfig(target_modules=["lin0"])
        model = get_peft_model(MLP(), config)
        print_output = repr(model.model.lin0)
        assert print_output.startswith("lora.Linear")
        assert "in_features=10" in print_output
        assert "out_features=20" in print_output
        assert "lora_A" in print_output
        assert "lora_B" in print_output
        assert "default" in print_output

    def test_repr_lora_embedding(self):
        config = LoraConfig(target_modules=["emb"])
        model = get_peft_model(ModelEmbConv1D(), config)
        print_output = repr(model.model.emb)
        assert print_output.startswith("lora.Embedding")
        assert "100, 5" in print_output
        assert "lora_embedding_A" in print_output
        assert "lora_embedding_B" in print_output
        assert "default" in print_output

    def test_repr_lora_conv1d(self):
        config = LoraConfig(target_modules=["conv1d"])
        model = get_peft_model(ModelEmbConv1D(), config)
        print_output = repr(model.model.conv1d)
        assert print_output.startswith("lora.Linear")
        assert "in_features=5" in print_output
        assert "out_features=1" in print_output
        assert "lora_A" in print_output
        assert "lora_B" in print_output
        assert "default" in print_output

    def test_repr_lora_conv2d(self):
        config = LoraConfig(target_modules=["conv2d"])
        model = get_peft_model(ModelConv2D(), config)
        print_output = repr(model.model.conv2d)
        assert print_output.startswith("lora.Conv2d")
        assert "5, 10" in print_output
        assert "kernel_size=(3, 3)" in print_output
        assert "stride=(1, 1)" in print_output
        assert "lora_A" in print_output
        assert "lora_B" in print_output
        assert "default" in print_output


class TestMultipleActiveAdapters:
    """
    A test class to test the functionality of multiple active adapters.

    This is not specifically tied to custom models, it's just easy to test here and testing it on all types of models
    would be overkill.
    """

    torch_device = infer_device()

    def prepare_inputs_for_testing(self):
        X = torch.arange(90).view(9, 10).to(self.torch_device)
        return {"X": X}

    def set_multiple_active_adapters(self, model, adapter_names):
        for module in model.modules():
            if isinstance(module, (BaseTunerLayer, AuxiliaryTrainingWrapper)):
                module.set_adapter(adapter_names)

    def resolve_model_cls(self, tuner_method):
        if tuner_method == "lora+trainable_tokens":
            # for this method we need an Embedding layer to target
            return ModelEmbConv1D()
        if tuner_method == "ia3":
            return MLP(bias=False)
        return MLP(bias=True)

    @pytest.mark.parametrize(
        "test_name, tuner_method, config_cls, config_kwargs_1, config_kwargs_2", MULTIPLE_ACTIVE_ADAPTERS_TEST_CASES
    )
    def test_multiple_active_adapters_forward(
        self, test_name, tuner_method, config_cls, config_kwargs_1, config_kwargs_2
    ):
        torch.manual_seed(0)

        model = self.resolve_model_cls(tuner_method)
        model = model.to(self.torch_device).eval()

        X = self.prepare_inputs_for_testing()

        config_1 = config_cls(**config_kwargs_1)
        config_2 = config_cls(**config_kwargs_2)

        peft_model = get_peft_model(model, config_1, adapter_name="adapter_1")
        peft_model.add_adapter("adapter_2", config_2)

        # the assumption that the output of the combined output of two adapters is != to the output of one
        # adapter is not true for unmodified trainable tokens as they just mimic the existing embedding matrix.
        # therefore, we modify the weights so that the adapter weights differs from the embedding weights.
        #
        # We do it this way because we have no way to pass something like `init_weights=False` to the token adapter.
        if "trainable_tokens" in tuner_method:
            peft_model.emb.token_adapter.trainable_tokens_delta["adapter_1"].data = torch.rand_like(
                peft_model.emb.token_adapter.trainable_tokens_delta["adapter_1"].data
            )
            peft_model.emb.token_adapter.trainable_tokens_delta["adapter_2"].data = torch.rand_like(
                peft_model.emb.token_adapter.trainable_tokens_delta["adapter_2"].data
            )

        # set adapter_1
        peft_model.set_adapter("adapter_1")
        adapter_1_output = peft_model(**X)

        # set adapter_2
        peft_model.set_adapter("adapter_2")
        adapter_2_output = peft_model(**X)

        # set ["adapter_1", "adapter_2"]
        self.set_multiple_active_adapters(peft_model, ["adapter_1", "adapter_2"])
        combined_output = peft_model(**X)

        assert not torch.allclose(adapter_1_output, adapter_2_output, atol=1e-5)
        assert not torch.allclose(adapter_1_output, combined_output, atol=1e-5)
        assert not torch.allclose(adapter_2_output, combined_output, atol=1e-5)

        if (tuner_method == "lora") and not (config_1.target_parameters or config_2.target_parameters):
            # Create a weighted adapter combining both adapters and check that its output is same as setting multiple
            # active adapters. `target_parameters` is not supported.
            peft_model.add_weighted_adapter(
                ["adapter_1", "adapter_2"], [1.0, 1.0], "new_combined_adapter", combination_type="cat"
            )
            peft_model.set_adapter("new_combined_adapter")
            new_combined_output = peft_model(**X)
            assert torch.allclose(new_combined_output, combined_output, atol=1e-5)

    @pytest.mark.parametrize(
        "test_name, tuner_method, config_cls, config_kwargs_1, config_kwargs_2", MULTIPLE_ACTIVE_ADAPTERS_TEST_CASES
    )
    def test_multiple_active_adapters_merge_and_unmerge(
        self, test_name, tuner_method, config_cls, config_kwargs_1, config_kwargs_2
    ):
        torch.manual_seed(0)

        model = self.resolve_model_cls(tuner_method)
        model = model.to(self.torch_device).eval()

        X = self.prepare_inputs_for_testing()
        base_output = model(**X)

        config_1 = config_cls(**config_kwargs_1)
        config_2 = config_cls(**config_kwargs_2)

        peft_model = get_peft_model(model, config_1, adapter_name="adapter_1")
        peft_model.add_adapter("adapter_2", config_2)

        # set ["adapter_1", "adapter_2"]
        self.set_multiple_active_adapters(peft_model, ["adapter_1", "adapter_2"])
        combined_output = peft_model(**X)

        peft_model.merge_adapter()
        merged_combined_output = peft_model(**X)
        assert torch.allclose(merged_combined_output, combined_output, atol=1e-4)

        peft_model.unmerge_adapter()

        with peft_model.disable_adapter():
            disabled_adapter_output = peft_model(**X)

        assert torch.allclose(disabled_adapter_output, base_output, atol=1e-4)

    @pytest.mark.parametrize(
        "test_name, tuner_method, config_cls, config_kwargs_1, config_kwargs_2", MULTIPLE_ACTIVE_ADAPTERS_TEST_CASES
    )
    def test_merge_layers_multi(self, test_name, tuner_method, config_cls, config_kwargs_1, config_kwargs_2):
        torch.manual_seed(0)

        model = self.resolve_model_cls(tuner_method)
        model = model.to(self.torch_device).eval()

        config_1 = config_cls(**config_kwargs_1)
        config_2 = config_cls(**config_kwargs_2)

        model = get_peft_model(model, config_1)

        # the assumption that the output of the combined output of two adapters is != to the output of one
        # adapter is not true for unmodified trainable tokens as they just mimic the existing embedding matrix.
        # therefore, we modify the weights so that the adapter weights differs from the embedding weights. in this
        # case we even use 20*rand to be very distinct to adapter 2 since we're comparing outputs and not embeddings
        # with rather high tolerance values. this is also the reason why `init_weights` is not sufficient here and
        # when using `<peft method>.trainable_token_indices` we do not have the utility of `init_weights` anyway.
        if "trainable_tokens" in tuner_method:
            model.emb.token_adapter.trainable_tokens_delta["default"].data = 20 * torch.rand_like(
                model.emb.token_adapter.trainable_tokens_delta["default"].data
            )

        dummy_input = self.prepare_inputs_for_testing()
        model.eval()

        with torch.inference_mode():
            logits_adapter_1 = model(**dummy_input)[0]

        model.add_adapter("adapter-2", config_2)
        model.set_adapter("adapter-2")

        # same as above but for adapter 2
        if "trainable_tokens" in tuner_method:
            model.emb.token_adapter.trainable_tokens_delta["adapter-2"].data = 2 * torch.rand_like(
                model.emb.token_adapter.trainable_tokens_delta["adapter-2"].data
            )

        model.eval()

        with torch.inference_mode():
            logits_adapter_2 = model(**dummy_input)[0]

        assert not torch.allclose(logits_adapter_1, logits_adapter_2, atol=1e-3, rtol=1e-3)

        model.set_adapter("default")

        with torch.inference_mode():
            logits_adapter_1_after_set = model(**dummy_input)[0]

        assert torch.allclose(logits_adapter_1_after_set, logits_adapter_1, atol=1e-3, rtol=1e-3)

        model_copy = copy.deepcopy(model)
        model_copy_2 = copy.deepcopy(model)
        model_merged_all = model.merge_and_unload(adapter_names=["adapter-2", "default"])

        with torch.inference_mode():
            logits_merged_all = model_merged_all(**dummy_input)[0]

        assert not torch.allclose(logits_merged_all, logits_adapter_2, atol=1e-3, rtol=1e-3)
        assert not torch.allclose(logits_merged_all, logits_adapter_1, atol=1e-3, rtol=1e-3)

        model_merged_adapter_2 = model_copy.merge_and_unload(adapter_names=["adapter-2"])

        with torch.inference_mode():
            logits_merged_adapter_2 = model_merged_adapter_2(**dummy_input)[0]

        assert torch.allclose(logits_merged_adapter_2, logits_adapter_2, atol=1e-3, rtol=1e-3)

        model_merged_adapter_default = model_copy_2.merge_and_unload(adapter_names=["default"])

        with torch.inference_mode():
            logits_merged_adapter_default = model_merged_adapter_default(**dummy_input)[0]

        assert torch.allclose(logits_merged_adapter_default, logits_adapter_1, atol=1e-3, rtol=1e-3)


class TestRequiresGrad:
    """Test that requires_grad is set correctly in specific circumstances

    # See issue #899.

    This is not specifically tied to custom models, it's just easy to test here and testing it on all types of models
    would be overkill.

    """

    def check_requires_grad(self, model, *params_expected: str):
        # Check that only the given parameters have requires_grad=True, and all others have requires_grad=False.
        # Calling without arguments besides the model means that all parameters should have requires_grad=False.
        params_with_requires_grad = [name for name, param in model.named_parameters() if param.requires_grad]
        diff = set(params_expected).symmetric_difference(set(params_with_requires_grad))
        msg = f"Expected {params_expected} to require gradients, got {params_with_requires_grad}"
        assert len(diff) == 0, msg

    def test_requires_grad_modules_to_save_default(self):
        config = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        peft_model = get_peft_model(MLP(), config)

        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.modules_to_save.default.weight",
            "base_model.model.lin1.modules_to_save.default.bias",
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

    def test_requires_grad_modules_to_save_disabling(self):
        config = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        peft_model = get_peft_model(MLP(), config)

        # when disabling the adapter, the original module's grad should be enabled and vice versa
        peft_model.disable_adapter_layers()
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.original_module.weight",
            "base_model.model.lin1.original_module.bias",
        )

        # when re-enabling the adapter, the original module's grad should be disabled and vice versa
        peft_model.enable_adapter_layers()
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.modules_to_save.default.weight",
            "base_model.model.lin1.modules_to_save.default.bias",
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

        # when using the disable_adapter context, the original module's grad should be enabled and vice versa
        with peft_model.disable_adapter():
            self.check_requires_grad(
                peft_model,
                "base_model.model.lin1.original_module.weight",
                "base_model.model.lin1.original_module.bias",
            )

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.modules_to_save.default.weight",
            "base_model.model.lin1.modules_to_save.default.bias",
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

    def test_requires_grad_modules_to_save_multiple_adapters(self):
        config0 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.modules_to_save.default.weight",
            "base_model.model.lin1.modules_to_save.default.bias",
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.modules_to_save.default.weight",
            "base_model.model.lin1.modules_to_save.default.bias",
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

        # set config1 as active, should lead to adapter1 requiring grad
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.modules_to_save.adapter1.weight",
            "base_model.model.lin1.modules_to_save.adapter1.bias",
            "base_model.model.lin0.lora_A.adapter1.weight",
            "base_model.model.lin0.lora_B.adapter1.weight",
        )

    def test_requires_grad_lora_different_targets(self):
        # test two different LoRA adapters that target different modules
        config0 = LoraConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoraConfig(target_modules=["lin1"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.lora_A.adapter1.weight",
            "base_model.model.lin1.lora_B.adapter1.weight",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.lora_A.adapter1.weight",
            "base_model.model.lin1.lora_B.adapter1.weight",
        )

    def test_requires_grad_lora_same_targets(self):
        # same as previous test, except that LoRA adapters target the same layer
        config0 = LoraConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoraConfig(target_modules=["lin0"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.default.weight",
            "base_model.model.lin0.lora_B.default.weight",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.adapter1.weight",
            "base_model.model.lin0.lora_B.adapter1.weight",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.adapter1.weight",
            "base_model.model.lin0.lora_B.adapter1.weight",
        )

    def test_requires_grad_ia3_different_targets(self):
        # test two different IA3 adapters that target different modules
        config0 = IA3Config(target_modules=["lin0"], feedforward_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = IA3Config(target_modules=["lin1"], feedforward_modules=["lin1"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.ia3_l.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.ia3_l.adapter1",
        )

    def test_requires_grad_ia3_same_targets(self):
        # same as previous test, except that IA3 adapters target the same layer
        config0 = IA3Config(target_modules=["lin0"], feedforward_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = IA3Config(target_modules=["lin0"], feedforward_modules=["lin0"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.adapter1",
        )

    def test_requires_grad_adalora_different_targets(self):
        # test two different AdaLora adapters that target different modules
        config0 = AdaLoraConfig(target_modules=["lin0"], total_step=1)
        peft_model = get_peft_model(MLP(), config0)

        config1 = AdaLoraConfig(target_modules=["lin1"], total_step=1, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.default",
            "base_model.model.lin0.lora_B.default",
            "base_model.model.lin0.lora_E.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.default",
            "base_model.model.lin0.lora_B.default",
            "base_model.model.lin0.lora_E.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.lora_A.adapter1",
            "base_model.model.lin1.lora_B.adapter1",
            "base_model.model.lin1.lora_E.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.lora_A.adapter1",
            "base_model.model.lin1.lora_B.adapter1",
            "base_model.model.lin1.lora_E.adapter1",
        )

    def test_requires_grad_adalora_same_targets(self):
        # same as previous test, except that AdaLora adapters target the same layer
        config0 = AdaLoraConfig(target_modules=["lin0"], total_step=1)
        peft_model = get_peft_model(MLP(), config0)

        config1 = AdaLoraConfig(target_modules=["lin0"], total_step=1, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.default",
            "base_model.model.lin0.lora_B.default",
            "base_model.model.lin0.lora_E.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.default",
            "base_model.model.lin0.lora_B.default",
            "base_model.model.lin0.lora_E.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.adapter1",
            "base_model.model.lin0.lora_B.adapter1",
            "base_model.model.lin0.lora_E.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.adapter1",
            "base_model.model.lin0.lora_B.adapter1",
            "base_model.model.lin0.lora_E.adapter1",
        )

    def test_requires_grad_lora_conv2d(self):
        # test two different LoRA adapters that target different modules
        config0 = LoraConfig(target_modules=["conv2d"])
        peft_model = get_peft_model(ModelConv2D(), config0)

        config1 = LoraConfig(target_modules=["lin0"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.conv2d.lora_A.default.weight",
            "base_model.model.conv2d.lora_B.default.weight",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.conv2d.lora_A.default.weight",
            "base_model.model.conv2d.lora_B.default.weight",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.adapter1.weight",
            "base_model.model.lin0.lora_B.adapter1.weight",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lora_A.adapter1.weight",
            "base_model.model.lin0.lora_B.adapter1.weight",
        )

    def test_requires_grad_lora_emb_conv1d(self):
        # test two different LoRA adapters that target different modules
        config0 = LoraConfig(target_modules=["conv1d"])
        peft_model = get_peft_model(ModelEmbConv1D(), config0)

        config1 = LoraConfig(target_modules=["emb"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.conv1d.lora_A.default.weight",
            "base_model.model.conv1d.lora_B.default.weight",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.conv1d.lora_A.default.weight",
            "base_model.model.conv1d.lora_B.default.weight",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.emb.lora_embedding_A.adapter1",
            "base_model.model.emb.lora_embedding_B.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.emb.lora_embedding_A.adapter1",
            "base_model.model.emb.lora_embedding_B.adapter1",
        )

    def test_requires_grad_ia3_conv1d(self):
        # test two different LoRA adapters that target different modules
        config0 = IA3Config(target_modules=["conv1d"], feedforward_modules=[])
        peft_model = get_peft_model(ModelEmbConv1D(), config0)

        config1 = IA3Config(target_modules=["lin0"], feedforward_modules=["lin0"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.conv1d.ia3_l.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.conv1d.ia3_l.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.adapter1",
        )

    def test_requires_grad_ia3_conv2d(self):
        # test two different LoRA adapters that target different modules
        config0 = IA3Config(target_modules=["conv2d"], feedforward_modules=["conv2d"])
        peft_model = get_peft_model(ModelConv2D(), config0)

        config1 = IA3Config(target_modules=["lin0"], feedforward_modules=[])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.conv2d.ia3_l.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.conv2d.ia3_l.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.ia3_l.adapter1",
        )

    def test_requires_grad_loha_different_targets(self):
        # test two different LoHa adapters that target different modules
        config0 = LoHaConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoHaConfig(target_modules=["lin1"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hada_w1_a.default",
            "base_model.model.lin0.hada_w1_b.default",
            "base_model.model.lin0.hada_w2_a.default",
            "base_model.model.lin0.hada_w2_b.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hada_w1_a.default",
            "base_model.model.lin0.hada_w1_b.default",
            "base_model.model.lin0.hada_w2_a.default",
            "base_model.model.lin0.hada_w2_b.default",
        )

        # change activate pter to pter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.hada_w1_a.adapter1",
            "base_model.model.lin1.hada_w1_b.adapter1",
            "base_model.model.lin1.hada_w2_a.adapter1",
            "base_model.model.lin1.hada_w2_b.adapter1",
        )

        # disable all pters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.hada_w1_a.adapter1",
            "base_model.model.lin1.hada_w1_b.adapter1",
            "base_model.model.lin1.hada_w2_a.adapter1",
            "base_model.model.lin1.hada_w2_b.adapter1",
        )

    def test_requires_grad_loha_same_targets(self):
        # same as previous test, except that LoHa adapters target the same layer
        config0 = LoHaConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoHaConfig(target_modules=["lin0"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hada_w1_a.default",
            "base_model.model.lin0.hada_w1_b.default",
            "base_model.model.lin0.hada_w2_a.default",
            "base_model.model.lin0.hada_w2_b.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hada_w1_a.default",
            "base_model.model.lin0.hada_w1_b.default",
            "base_model.model.lin0.hada_w2_a.default",
            "base_model.model.lin0.hada_w2_b.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hada_w1_a.adapter1",
            "base_model.model.lin0.hada_w1_b.adapter1",
            "base_model.model.lin0.hada_w2_a.adapter1",
            "base_model.model.lin0.hada_w2_b.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hada_w1_a.adapter1",
            "base_model.model.lin0.hada_w1_b.adapter1",
            "base_model.model.lin0.hada_w2_a.adapter1",
            "base_model.model.lin0.hada_w2_b.adapter1",
        )

    def test_requires_grad_lokr_different_targets(self):
        # test two different LoKr adapters that target different modules
        config0 = LoKrConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoKrConfig(target_modules=["lin1"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lokr_w1.default",
            "base_model.model.lin0.lokr_w2.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lokr_w1.default",
            "base_model.model.lin0.lokr_w2.default",
        )

        # change activate pter to pter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.lokr_w1.adapter1",
            "base_model.model.lin1.lokr_w2.adapter1",
        )

        # disable all pters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.lokr_w1.adapter1",
            "base_model.model.lin1.lokr_w2.adapter1",
        )

    def test_requires_grad_lokr_same_targets(self):
        # same as previous test, except that LoKr adapters target the same layer
        config0 = LoKrConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoKrConfig(target_modules=["lin0"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lokr_w1.default",
            "base_model.model.lin0.lokr_w2.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lokr_w1.default",
            "base_model.model.lin0.lokr_w2.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lokr_w1.adapter1",
            "base_model.model.lin0.lokr_w2.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.lokr_w1.adapter1",
            "base_model.model.lin0.lokr_w2.adapter1",
        )

    def test_requires_grad_oft_different_targets(self):
        # test two different OFT adapters that target different modules
        config0 = OFTConfig(target_modules=["lin0"], r=2, oft_block_size=0)
        peft_model = get_peft_model(MLP(), config0)

        config1 = OFTConfig(target_modules=["lin1"], r=2, oft_block_size=0, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.oft_R.default.weight",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.oft_R.default.weight",
        )

        # change activate pter to pter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.oft_R.adapter1.weight",
        )

        # disable all pters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.oft_R.adapter1.weight",
        )

    def test_requires_grad_oft_same_targets(self):
        # same as previous test, except that OFT adapters target the same layer
        config0 = OFTConfig(target_modules=["lin0"], r=2, oft_block_size=0)
        peft_model = get_peft_model(MLP(), config0)

        config1 = OFTConfig(target_modules=["lin0"], r=2, oft_block_size=0, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.oft_R.default.weight",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.oft_R.default.weight",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.oft_R.adapter1.weight",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.oft_R.adapter1.weight",
        )

    def test_requires_grad_hra_different_targets(self):
        # test two different HRA adapters that target different modules
        config0 = HRAConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = HRAConfig(target_modules=["lin1"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hra_u.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hra_u.default",
        )

        # change activate pter to pter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.hra_u.adapter1",
        )

        # disable all pters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.hra_u.adapter1",
        )

    def test_requires_grad_hra_same_targets(self):
        # same as previous test, except that HRA adapters target the same layer
        config0 = HRAConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = HRAConfig(target_modules=["lin0"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hra_u.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hra_u.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hra_u.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.hra_u.adapter1",
        )

    def test_requires_grad_bone_different_targets(self):
        # test two different HRA adapters that target different modules
        config0 = BoneConfig(target_modules=["lin0"], r=2)
        peft_model = get_peft_model(MLP(), config0)

        config1 = BoneConfig(target_modules=["lin1"], r=2, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.bone_block.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.bone_block.default",
        )

        # change activate pter to pter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.bone_block.adapter1",
        )

        # disable all pters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.bone_block.adapter1",
        )

    def test_requires_grad_bone_same_targets(self):
        # same as previous test, except that HRA adapters target the same layer
        config0 = BoneConfig(target_modules=["lin0"], r=2)
        peft_model = get_peft_model(MLP(), config0)

        config1 = BoneConfig(target_modules=["lin0"], r=2, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.bone_block.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.bone_block.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.bone_block.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.bone_block.adapter1",
        )

    def test_requires_grad_miss_different_targets(self):
        # test two different HRA adapters that target different modules
        config0 = MissConfig(target_modules=["lin0"], r=2)
        peft_model = get_peft_model(MLP(), config0)

        config1 = MissConfig(target_modules=["lin1"], r=2, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.miss_block.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.miss_block.default",
        )

        # change activate pter to pter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.miss_block.adapter1",
        )

        # disable all pters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.miss_block.adapter1",
        )

    def test_requires_grad_miss_same_targets(self):
        # same as previous test, except that HRA adapters target the same layer
        config0 = MissConfig(target_modules=["lin0"], r=2)
        peft_model = get_peft_model(MLP(), config0)

        config1 = MissConfig(target_modules=["lin0"], r=2, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.miss_block.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.miss_block.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.miss_block.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.miss_block.adapter1",
        )

    def test_requires_grad_boft_different_targets(self):
        # test two different OFT adapters that target different modules
        config0 = BOFTConfig(target_modules=["lin0"], boft_block_size=2)
        peft_model = get_peft_model(MLP2(), config0)

        config1 = BOFTConfig(target_modules=["lin1"], boft_block_size=2, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active pter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.boft_R.default",
            "base_model.model.lin0.boft_s.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.boft_R.default",
            "base_model.model.lin0.boft_s.default",
        )

        # change activate pter to pter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.boft_R.adapter1",
            "base_model.model.lin1.boft_s.adapter1",
        )

        # disable all pters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.boft_R.adapter1",
            "base_model.model.lin1.boft_s.adapter1",
        )

    def test_requires_grad_boft_same_targets(self):
        # same as previous test, except that BOFT adapters target the same layer
        config0 = BOFTConfig(target_modules=["lin1"], boft_block_size=2)
        peft_model = get_peft_model(MLP(), config0)

        config1 = BOFTConfig(target_modules=["lin1"], boft_block_size=2, inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.boft_R.default",
            "base_model.model.lin1.boft_s.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.boft_R.default",
            "base_model.model.lin1.boft_s.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.boft_R.adapter1",
            "base_model.model.lin1.boft_s.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.boft_R.adapter1",
            "base_model.model.lin1.boft_s.adapter1",
        )

    def test_requires_grad_lntuning_different_targets(self):
        config0 = LNTuningConfig(
            target_modules=["layernorm0"],
        )
        peft_model = get_peft_model(MLP_LayerNorm(), config0)

        config1 = LNTuningConfig(
            target_modules=["layernorm1"],
            inference_mode=True,
        )
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.layernorm0.ln_tuning_layers.default.weight",
            "base_model.model.layernorm0.ln_tuning_layers.default.bias",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.layernorm0.ln_tuning_layers.default.weight",
            "base_model.model.layernorm0.ln_tuning_layers.default.bias",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.layernorm1.ln_tuning_layers.adapter1.weight",
            "base_model.model.layernorm1.ln_tuning_layers.adapter1.bias",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.layernorm1.ln_tuning_layers.adapter1.weight",
            "base_model.model.layernorm1.ln_tuning_layers.adapter1.bias",
        )

    def test_requires_grad_lntuning_same_targets(self):
        config0 = LNTuningConfig(
            target_modules=["layernorm0"],
        )
        peft_model = get_peft_model(MLP_LayerNorm(), config0)

        config1 = LNTuningConfig(target_modules=["layernorm0"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.layernorm0.ln_tuning_layers.default.weight",
            "base_model.model.layernorm0.ln_tuning_layers.default.bias",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.layernorm0.ln_tuning_layers.default.weight",
            "base_model.model.layernorm0.ln_tuning_layers.default.bias",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.layernorm0.ln_tuning_layers.adapter1.weight",
            "base_model.model.layernorm0.ln_tuning_layers.adapter1.bias",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.layernorm0.ln_tuning_layers.adapter1.weight",
            "base_model.model.layernorm0.ln_tuning_layers.adapter1.bias",
        )

    def test_requires_grad_vera_different_targets(self):
        # Test two different VeRA adapters that target different modules. Most notably, ensure that vera_A and vera_B
        # don't require grads.

        # requires a model with at least 2 layers with the same shapes
        class MLP2(nn.Module):
            def __init__(self, bias=True):
                super().__init__()
                self.relu = nn.ReLU()
                self.lin0 = nn.Linear(10, 20, bias=bias)
                self.lin1 = nn.Linear(20, 20, bias=bias)  # lin1 and lin2 have same shape
                self.lin2 = nn.Linear(20, 20, bias=bias)
                self.lin3 = nn.Linear(20, 2, bias=bias)
                self.sm = nn.LogSoftmax(dim=-1)

            def forward(self, X):
                X = X.float()
                X = self.lin0(X)
                X = self.relu(X)
                X = self.lin1(X)
                X = self.relu(X)
                X = self.lin2(X)
                X = self.relu(X)
                X = self.lin3(X)
                X = self.sm(X)
                return X

        config0 = VeraConfig(target_modules=["lin1"])
        peft_model = get_peft_model(MLP2(), config0)

        config1 = VeraConfig(target_modules=["lin2"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.vera_lambda_b.default",
            "base_model.model.lin1.vera_lambda_d.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.vera_lambda_b.default",
            "base_model.model.lin1.vera_lambda_d.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin2.vera_lambda_b.adapter1",
            "base_model.model.lin2.vera_lambda_d.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin2.vera_lambda_b.adapter1",
            "base_model.model.lin2.vera_lambda_d.adapter1",
        )

    def test_requires_grad_vera_same_targets(self):
        # Test two different VeRA adapters that target the same module. Most notably, ensure that vera_A and vera_B
        # don't require grads.

        # requires a model with at least 2 layers with the same shapes
        class MLP2(nn.Module):
            def __init__(self, bias=True):
                super().__init__()
                self.relu = nn.ReLU()
                self.lin0 = nn.Linear(10, 20, bias=bias)
                self.lin1 = nn.Linear(20, 20, bias=bias)  # lin1 and lin2 have same shape
                self.lin2 = nn.Linear(20, 20, bias=bias)
                self.lin3 = nn.Linear(20, 2, bias=bias)
                self.sm = nn.LogSoftmax(dim=-1)

            def forward(self, X):
                X = X.float()
                X = self.lin0(X)
                X = self.relu(X)
                X = self.lin1(X)
                X = self.relu(X)
                X = self.lin2(X)
                X = self.relu(X)
                X = self.lin3(X)
                X = self.sm(X)
                return X

        config0 = VeraConfig(target_modules=["lin1", "lin2"])
        peft_model = get_peft_model(MLP2(), config0)

        config1 = VeraConfig(target_modules=["lin1", "lin2"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.vera_lambda_b.default",
            "base_model.model.lin1.vera_lambda_d.default",
            "base_model.model.lin2.vera_lambda_b.default",
            "base_model.model.lin2.vera_lambda_d.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.vera_lambda_b.default",
            "base_model.model.lin1.vera_lambda_d.default",
            "base_model.model.lin2.vera_lambda_b.default",
            "base_model.model.lin2.vera_lambda_d.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.vera_lambda_b.adapter1",
            "base_model.model.lin1.vera_lambda_d.adapter1",
            "base_model.model.lin2.vera_lambda_b.adapter1",
            "base_model.model.lin2.vera_lambda_d.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.vera_lambda_b.adapter1",
            "base_model.model.lin1.vera_lambda_d.adapter1",
            "base_model.model.lin2.vera_lambda_b.adapter1",
            "base_model.model.lin2.vera_lambda_d.adapter1",
        )

    def test_requires_grad_randlora_different_targets(self):
        # Test two different RandLora adapters that target different modules. Most notably, ensure that randbasis_A and randbasis_B
        # don't require grads.

        # requires a model with at least 2 layers with the same shapes
        class MLP2(nn.Module):
            def __init__(self, bias=True):
                super().__init__()
                self.relu = nn.ReLU()
                self.lin0 = nn.Linear(10, 20, bias=bias)
                self.lin1 = nn.Linear(20, 20, bias=bias)  # lin1 and lin2 have same shape
                self.lin2 = nn.Linear(20, 20, bias=bias)
                self.lin3 = nn.Linear(20, 2, bias=bias)
                self.sm = nn.LogSoftmax(dim=-1)

            def forward(self, X):
                X = X.float()
                X = self.lin0(X)
                X = self.relu(X)
                X = self.lin1(X)
                X = self.relu(X)
                X = self.lin2(X)
                X = self.relu(X)
                X = self.lin3(X)
                X = self.sm(X)
                return X

        config0 = RandLoraConfig(target_modules=["lin1"])
        peft_model = get_peft_model(MLP2(), config0)

        config1 = RandLoraConfig(target_modules=["lin2"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.randlora_lambda.default",
            "base_model.model.lin1.randlora_gamma.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.randlora_lambda.default",
            "base_model.model.lin1.randlora_gamma.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin2.randlora_lambda.adapter1",
            "base_model.model.lin2.randlora_gamma.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin2.randlora_lambda.adapter1",
            "base_model.model.lin2.randlora_gamma.adapter1",
        )

    def test_requires_grad_randlora_same_targets(self):
        # Test two different RandLora adapters that target the same module. Most notably, ensure that randbasis_A and randbasis_B
        # don't require grads.

        # requires a model with at least 2 layers with the same shapes
        class MLP2(nn.Module):
            def __init__(self, bias=True):
                super().__init__()
                self.relu = nn.ReLU()
                self.lin0 = nn.Linear(10, 20, bias=bias)
                self.lin1 = nn.Linear(20, 20, bias=bias)  # lin1 and lin2 have same shape
                self.lin2 = nn.Linear(20, 20, bias=bias)
                self.lin3 = nn.Linear(20, 2, bias=bias)
                self.sm = nn.LogSoftmax(dim=-1)

            def forward(self, X):
                X = X.float()
                X = self.lin0(X)
                X = self.relu(X)
                X = self.lin1(X)
                X = self.relu(X)
                X = self.lin2(X)
                X = self.relu(X)
                X = self.lin3(X)
                X = self.sm(X)
                return X

        config0 = RandLoraConfig(target_modules=["lin1", "lin2"])
        peft_model = get_peft_model(MLP2(), config0)

        config1 = RandLoraConfig(target_modules=["lin1", "lin2"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.randlora_lambda.default",
            "base_model.model.lin1.randlora_gamma.default",
            "base_model.model.lin2.randlora_lambda.default",
            "base_model.model.lin2.randlora_gamma.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.randlora_lambda.default",
            "base_model.model.lin1.randlora_gamma.default",
            "base_model.model.lin2.randlora_lambda.default",
            "base_model.model.lin2.randlora_gamma.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.randlora_lambda.adapter1",
            "base_model.model.lin1.randlora_gamma.adapter1",
            "base_model.model.lin2.randlora_lambda.adapter1",
            "base_model.model.lin2.randlora_gamma.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.randlora_lambda.adapter1",
            "base_model.model.lin1.randlora_gamma.adapter1",
            "base_model.model.lin2.randlora_lambda.adapter1",
            "base_model.model.lin2.randlora_gamma.adapter1",
        )

    def test_requires_grad_vblora_different_targets(self):
        # test two different VBLoRA adapters that target different modules
        config0 = VBLoRAConfig(target_modules=["lin0"], vector_length=1, num_vectors=2)
        peft_model = get_peft_model(MLP(), config0)

        config1 = VBLoRAConfig(target_modules=["lin1"], vector_length=1, num_vectors=2)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.vblora_logits_A.default",
            "base_model.model.lin0.vblora_logits_B.default",
            "base_model.model.lin0.vblora_vector_bank.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.vblora_logits_A.default",
            "base_model.model.lin0.vblora_logits_B.default",
            "base_model.model.lin0.vblora_vector_bank.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.vblora_logits_A.adapter1",
            "base_model.model.lin1.vblora_logits_B.adapter1",
            "base_model.model.lin0.vblora_vector_bank.adapter1",  # vblora_vector_bank is shared
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.vblora_logits_A.adapter1",
            "base_model.model.lin1.vblora_logits_B.adapter1",
            "base_model.model.lin0.vblora_vector_bank.adapter1",  # vblora_vector_bank is shared
        )

    def test_requires_grad_vblora_same_targets(self):
        # same as previous test, except that VBLoRA adapters target the same layer
        config0 = VBLoRAConfig(target_modules=["lin0"], vector_length=1, num_vectors=2)
        peft_model = get_peft_model(MLP(), config0)

        config1 = VBLoRAConfig(target_modules=["lin0"], vector_length=1, num_vectors=2)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.vblora_logits_A.default",
            "base_model.model.lin0.vblora_logits_B.default",
            "base_model.model.lin0.vblora_vector_bank.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.vblora_logits_A.default",
            "base_model.model.lin0.vblora_logits_B.default",
            "base_model.model.lin0.vblora_vector_bank.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.vblora_logits_A.adapter1",
            "base_model.model.lin0.vblora_logits_B.adapter1",
            "base_model.model.lin0.vblora_vector_bank.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.vblora_logits_A.adapter1",
            "base_model.model.lin0.vblora_logits_B.adapter1",
            "base_model.model.lin0.vblora_vector_bank.adapter1",
        )

    def test_requires_grad_fourierft_different_targets(self):
        # test two different fourierft adapters that target different modules
        config0 = FourierFTConfig(n_frequency=10, target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = FourierFTConfig(n_frequency=10, target_modules=["lin1"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.fourierft_spectrum.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.fourierft_spectrum.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.fourierft_spectrum.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin1.fourierft_spectrum.adapter1",
        )

    def test_requires_grad_fourierft_same_targets(self):
        # same as previous test, except that AdaLora adapters target the same layer
        config0 = FourierFTConfig(n_frequency=10, target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = FourierFTConfig(n_frequency=10, target_modules=["lin0"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.fourierft_spectrum.default",
        )

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.fourierft_spectrum.default",
        )

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.fourierft_spectrum.adapter1",
        )

        # disable all adapters
        with peft_model.disable_adapter():
            self.check_requires_grad(peft_model)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.check_requires_grad(
            peft_model,
            "base_model.model.lin0.fourierft_spectrum.adapter1",
        )


class TestMixedAdapterBatches:
    torch_device = infer_device()

    @pytest.fixture
    def mlp_lora(self):
        """A simple MLP with 2 LoRA adapters"""
        torch.manual_seed(0)

        base_model = MLP().to(self.torch_device).eval()
        config0 = LoraConfig(target_modules=["lin0"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["lin0"], r=16, init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)
        return peft_model

    def run_checks(self, model, inputs):
        # This checks that we can have mixed adapters in a single batch. The test works by creating the outputs for the
        # base model, adapter 0, and adapter 1 separately. Then, we create an output with mixed adapters, where the
        # sample [0, 3, 6] are for the base model, [1, 4, 7] for adapter 0, and [2, 5, 8] for adapter 1. Finally, we
        # check that the outputs of the mixed batch are correct for the corresponding indices.
        adapter_name0, adapter_name1 = model.peft_config.keys()

        with model.disable_adapter():
            output_base = model(**inputs)

        model.set_adapter(adapter_name0)
        output0 = model(**inputs)

        # sanity check, outputs are not the same
        assert not torch.allclose(output_base, output0)

        model.set_adapter(adapter_name1)
        output1 = model(**inputs)

        # sanity check, outputs have the right shape and are not the same
        assert len(output_base) >= 3
        assert len(output_base) == len(output0) == len(output1)
        assert not torch.allclose(output_base, output0)
        assert not torch.allclose(output_base, output1)

        # set adapter_indices so that it alternates between base, adapter 0, and adapter 1
        adapters = ["__base__", adapter_name0, adapter_name1]
        inputs["adapter_names"] = [adapters[i % 3] for i in (range(len(inputs["X"])))]
        output_mixed = model.forward(**inputs)

        assert torch.allclose(output_base[::3], output_mixed[::3])
        assert torch.allclose(output0[1::3], output_mixed[1::3])
        assert torch.allclose(output1[2::3], output_mixed[2::3])

    def test_mixed_adapter_batches_lora_mlp(self, mlp_lora):
        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        self.run_checks(mlp_lora, inputs)

    def test_mixed_adapter_batches_lora_different_target_layers(self, mlp_lora):
        base_model = MLP().to(self.torch_device).eval()
        config0 = LoraConfig(target_modules=["lin0"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["lin1"], init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)
        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        self.run_checks(peft_model, inputs)

    def test_mixed_adapter_batches_lora_multiple_modules_to_save(self, mlp_lora):
        base_model = MLP().to(self.torch_device).eval()
        config0 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"], init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)
        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        self.run_checks(peft_model, inputs)

    def test_mixed_adapter_batches_lora_unsupported_layer_raises(self, mlp_lora):
        base_model = MLPWithGRU().to(self.torch_device).eval()
        config0 = LoraConfig(target_modules=["lin0"], modules_to_save=["gru"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["lin0"], modules_to_save=["gru"], init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)
        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        SUPPORTED_MODULES = (torch.nn.Linear, torch.nn.Embedding, torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d)
        module_names = ", ".join([module.__name__ for module in SUPPORTED_MODULES])
        with pytest.raises(
            TypeError, match=f"Mixed batching is only supported for the following modules: {module_names}."
        ):
            self.run_checks(peft_model, inputs)

    def test_mixed_adapter_batches_lora_partly_overlapping_target_layers(self, mlp_lora):
        base_model = MLP().to(self.torch_device).eval()
        # target different lora layers
        config0 = LoraConfig(target_modules=["lin0"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["lin0", "lin1"], init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)

        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        self.run_checks(peft_model, inputs)

    def test_mixed_adapter_batches_lora_conv1d_emb(self):
        base_model = ModelEmbConv1D().to(self.torch_device).eval()
        config0 = LoraConfig(target_modules=["emb", "conv1d"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["emb", "conv1d"], r=16, init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)

        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        self.run_checks(peft_model, inputs)

    def test_mixed_adapter_batches_lora_conv1d_emb_multiple_modules_to_save(self):
        base_model = ModelEmbConv1D().to(self.torch_device).eval()
        config0 = LoraConfig(target_modules=["emb", "conv1d"], modules_to_save=["lin0"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["emb", "conv1d"], modules_to_save=["lin0"], init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)
        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        self.run_checks(peft_model, inputs)

    def test_mixed_adapter_batches_lora_conv2d(self):
        base_model = ModelConv2D().to(self.torch_device).eval()
        config0 = LoraConfig(target_modules=["conv2d"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["conv2d"], r=16, init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)

        inputs = {"X": torch.arange(270).view(6, 5, 3, 3).to(self.torch_device)}
        self.run_checks(peft_model, inputs)

    def test_mixed_adapter_batches_mha_raises(self):
        base_model = ModelMha().to(self.torch_device).eval()
        config0 = LoraConfig(target_modules=["mha"], init_lora_weights=False)
        config1 = LoraConfig(target_modules=["mha"], r=16, init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter0").eval()
        peft_model.add_adapter("adapter1", config1)

        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        msg = "lora.MultiheadAttention does not support mixed adapter batches"
        with pytest.raises(TypeError, match=msg):
            self.run_checks(peft_model, inputs)

    def test_mixed_adapter_batches_lora_length_mismatch_raises(self, mlp_lora):
        inputs = {
            "X": torch.arange(90).view(-1, 10).to(self.torch_device),
            "adapter_names": ["__base__"] * 5,  # wrong length!
        }
        msg = r"Length of `adapter_names` should be the same as the number of inputs, but got "
        with pytest.raises(ValueError, match=msg):
            mlp_lora.forward(**inputs)

    def test_mixed_adapter_batches_lora_training_mode_raises(self, mlp_lora):
        inputs = {
            "X": torch.arange(90).view(-1, 10).to(self.torch_device),
            "adapter_names": ["__base__"] * 9,
        }
        mlp_lora = mlp_lora.train()
        msg = r"Cannot pass `adapter_names` when the model is in training mode."
        with pytest.raises(ValueError, match=msg):
            mlp_lora.forward(**inputs)

    def test_mixed_adapter_batches_lora_disabled(self, mlp_lora):
        # Disabling adapters should have precedence over passing adapter names
        inputs = {"X": torch.arange(90).view(-1, 10).to(self.torch_device)}
        with mlp_lora.disable_adapter():
            output_disabled = mlp_lora(**inputs)

        adapters = ["__base__", "adapter0", "adapter1"]
        inputs["adapter_names"] = [adapters[i % 3] for i in (range(len(inputs["X"])))]
        with mlp_lora.disable_adapter():
            output_mixed = mlp_lora.forward(**inputs)

        assert torch.allclose(output_disabled, output_mixed)

    def test_mixed_adapter_batches_lora_merged_raises(self, mlp_lora):
        # When there are merged adapters, passing adapter names should raise an error
        inputs = {
            "X": torch.arange(90).view(-1, 10).to(self.torch_device),
            "adapter_names": ["adapter0"] * 9,
        }
        mlp_lora.merge_adapter(["adapter0"])
        msg = r"Cannot pass `adapter_names` when there are merged adapters, please call `unmerge_adapter` first."
        with pytest.raises(ValueError, match=msg):
            mlp_lora.forward(**inputs)

    def test_mixed_adapter_batches_lora_wrong_adapter_name_raises(self):
        # Ensure that all of the adapter names that are being passed actually exist
        torch.manual_seed(0)
        x = torch.arange(90).view(-1, 10).to(self.torch_device)

        base_model = MLP().to(self.torch_device).eval()
        config = LoraConfig(target_modules=["lin0"], init_lora_weights=False)
        peft_model = get_peft_model(base_model, config).eval()
        peft_model.add_adapter(adapter_name="other", peft_config=config)

        # sanity check: this works
        peft_model.forward(x, adapter_names=["default"] * 5 + ["other"] * 4)

        # check one correct and one incorrect adapter
        msg = re.escape("Trying to infer with non-existing adapter(s): does-not-exist")
        with pytest.raises(ValueError, match=msg):
            peft_model.forward(x, adapter_names=["default"] * 5 + ["does-not-exist"] * 4)

        # check two correct adapters and one incorrect adapter
        with pytest.raises(ValueError, match=msg):
            peft_model.forward(x, adapter_names=["default"] * 3 + ["does-not-exist"] * 4 + ["other"] * 2)

        # check only incorrect adapters
        msg = re.escape("Trying to infer with non-existing adapter(s): does-not-exist, other-does-not-exist")
        with pytest.raises(ValueError, match=msg):
            peft_model.forward(x, adapter_names=["does-not-exist"] * 5 + ["other-does-not-exist"] * 4)

    def test_mixed_adapter_batches_lora_with_dora_raises(self):
        # When there are DoRA adapters, passing adapter names should raise an error
        torch.manual_seed(0)
        inputs = {
            "X": torch.arange(90).view(-1, 10).to(self.torch_device),
            "adapter_names": ["default"] * 9,
        }

        base_model = MLP().to(self.torch_device).eval()
        config = LoraConfig(target_modules=["lin0"], init_lora_weights=False, use_dora=True)
        peft_model = get_peft_model(base_model, config).eval()
        msg = r"Cannot pass `adapter_names` when DoRA is enabled."
        with pytest.raises(ValueError, match=msg):
            peft_model.forward(**inputs)

    def test_mixed_adapter_batches_lora_with_dora_but_dora_not_included_works(self):
        # When there are DoRA adapters, passing adapter names should raise an error, see previous test. However, when
        # the adapter that uses DoRA is not included in adapter_names, it's actually fine.
        torch.manual_seed(0)
        base_model = MLP().to(self.torch_device).eval()
        config_dora = LoraConfig(target_modules=["lin0"], init_lora_weights=False, use_dora=True)
        peft_model = get_peft_model(base_model, config_dora)
        config_no_dora = LoraConfig(target_modules=["lin0"], init_lora_weights=False, use_dora=False)
        peft_model.add_adapter(adapter_name="other", peft_config=config_no_dora)
        peft_model.eval()

        # The "default" adapter uses DoRA but "other" is not using it, so using "other" is fine. Also, "__base__" is
        # fine since it uses the base model and thus DoRA is not involved either.
        inputs = {
            "X": torch.arange(90).view(-1, 10).to(self.torch_device),
            "adapter_names": ["other"] * 4 + ["__base__"] * 5,
        }
        peft_model.forward(**inputs)

    @require_non_cpu
    def test_mixed_adapter_batches_lora_opt_timing(self):
        # Use a more realistic model (opt-125m) and do a simple runtime check to ensure that mixed adapter batches
        # don't add too much overhead. These types of tests are inherently flaky, so we try to add in some robustness.
        logs = []  # store the time it takes to run each forward pass here

        @contextmanager
        def timed():
            tic = time.perf_counter()
            yield
            toc = time.perf_counter()
            logs.append(toc - tic)

        base_model = AutoModelForCausalLM.from_pretrained("facebook/opt-125m").to(self.torch_device).eval()
        inputs = {"input_ids": torch.randint(0, 1000, (16, 64)).to(self.torch_device)}
        with timed():
            output_base = base_model(**inputs).logits

        config0 = LoraConfig(task_type="CAUSAL_LM", init_lora_weights=False)
        peft_model = get_peft_model(base_model, config0, "adapter1").eval()
        with timed():
            output0 = peft_model(**inputs).logits

        # sanity check, outputs are not the same
        assert not torch.allclose(output_base, output0)

        config1 = LoraConfig(task_type="CAUSAL_LM", r=16, init_lora_weights=False)
        peft_model.add_adapter("adapter2", config1)
        peft_model.set_adapter("adapter2")
        with timed():
            output1 = peft_model(**inputs).logits

        # sanity check, outputs are not the same
        assert not torch.allclose(output_base, output1)

        # set adapter_indices so that it alternates between 0 (base), lora 1, and lora 2
        adapters = ["__base__", "adapter1", "adapter2"]
        inputs["adapter_names"] = [adapters[i % 3] for i in (range(len(inputs["input_ids"])))]
        with timed():
            output_mixed = peft_model.forward(**inputs).logits

        atol, rtol = 1e-4, 1e-4
        assert torch.allclose(output_base[::3], output_mixed[::3], atol=atol, rtol=rtol)
        assert torch.allclose(output0[1::3], output_mixed[1::3], atol=atol, rtol=rtol)
        assert torch.allclose(output1[2::3], output_mixed[2::3], atol=atol, rtol=rtol)

        # Check that the overhead in time added by mixed batches is not too high.
        # To prevent flakiness, we measure mixed inference 3 times and take the lowest value, then compare it to the mean
        # of the non-mixed inference times. We also grant a generous margin of 2x the mean time.
        with timed():
            output_mixed = peft_model.forward(**inputs).logits
        with timed():
            output_mixed = peft_model.forward(**inputs).logits

        time_base, time0, time1, *time_mixed = logs
        time_non_mixed = (time_base + time0 + time1) / 3
        time_mixed = min(time_mixed)

        factor = 2.0
        assert time_mixed < factor * time_non_mixed

        # Measure timing of running base and adapter separately vs using a mixed batch. Note that on CPU, the
        # differences are quite small, so this test requires GPU to avoid flakiness.
        for _ in range(3):
            with timed():
                with peft_model.disable_adapter():
                    peft_model(**{k: v[::3] for k, v in inputs.items()})
                peft_model.set_adapter("adapter1")
                peft_model(**{k: v[1::3] for k, v in inputs.items()})
                peft_model.set_adapter("adapter2")
                peft_model(**{k: v[2::3] for k, v in inputs.items()})

        times_separate = logs[-3:]
        time_separate = sum(times_separate) / 3
        assert time_separate > time_mixed


class TestDynamicDispatch:
    # These are tests for the dynamic dispatch feature for LoRA. We create a custom module and a custom LoRA layer
    # that targets it.

    @pytest.fixture(scope="class")
    def custom_module_cls(self):
        class MyModule(nn.Module):
            # A custom layer that just behaves like an nn.Linear layer but is not an instance of nn.Linear. Therefore,
            # it would normally fail to be targeted.
            def __init__(self):
                super().__init__()
                self.in_features = 10
                self.out_features = 20
                self.weight = nn.Parameter(torch.randn(20, 10))

            def forward(self, x):
                return nn.functional.linear(x, self.weight)

        return MyModule

    @pytest.fixture(scope="class")
    def custom_lora_cls(self):
        from peft.tuners import lora

        class MyLora(lora.Linear):
            # just re-use the lora.Linear code here
            pass

        return MyLora

    @pytest.fixture(scope="class")
    def model_cls(self, custom_module_cls):
        class MyModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.lin0 = nn.Linear(10, 10)
                self.relu = nn.ReLU()
                self.my_module = custom_module_cls()
                self.lin1 = nn.Linear(20, 2)

            def forward(self, x):
                x = self.relu(self.lin0(x))
                x = self.relu(self.my_module(x))
                x = self.lin1(x)
                return x

        return MyModel

    def test_custom_lora_layer_used(self, custom_module_cls, custom_lora_cls, model_cls):
        # check that when we register custom lora layers, they are indeed being used for the intended module
        model = model_cls()
        config = LoraConfig(target_modules=["lin0", "my_module", "lin1"])
        config._register_custom_module({custom_module_cls: custom_lora_cls})

        peft_model = get_peft_model(model, config)
        assert isinstance(peft_model.base_model.model.my_module, custom_lora_cls)
        assert isinstance(peft_model.base_model.model.my_module.base_layer, custom_module_cls)
        # sanity check that the other lora layer types are still the default ones
        assert not isinstance(peft_model.base_model.model.lin0.base_layer, custom_module_cls)
        assert not isinstance(peft_model.base_model.model.lin1.base_layer, custom_module_cls)

    def test_training_works(self, model_cls, custom_module_cls, custom_lora_cls):
        # check that when we train with custom lora layers, they are indeed updated
        model = model_cls()
        config = LoraConfig(target_modules=["lin0", "my_module", "lin1"])
        config._register_custom_module({custom_module_cls: custom_lora_cls})

        peft_model = get_peft_model(model, config)
        sd_before = copy.deepcopy(peft_model.state_dict())
        inputs = torch.randn(16, 10)
        optimizer = torch.optim.SGD(peft_model.parameters(), lr=1e-4)

        for _ in range(5):
            optimizer.zero_grad()
            output = peft_model(inputs)
            loss = output.sum() ** 2
            loss.backward()
            optimizer.step()

        sd_after = peft_model.state_dict()

        # sanity check that for finite results, since nan != nan, which would make the test pass trivially
        for val in sd_before.values():
            assert torch.isfinite(val).all()
        for val in sd_after.values():
            assert torch.isfinite(val).all()

        assert not torch.allclose(
            sd_before["base_model.model.my_module.lora_A.default.weight"],
            sd_after["base_model.model.my_module.lora_A.default.weight"],
        )
        assert not torch.allclose(
            sd_before["base_model.model.my_module.lora_B.default.weight"],
            sd_after["base_model.model.my_module.lora_B.default.weight"],
        )

    def test_saving_and_loading(self, custom_module_cls, custom_lora_cls, model_cls, tmp_path):
        # check that we can successfully save and load the custom lora cls
        torch.manual_seed(0)
        model = model_cls()
        config = LoraConfig(target_modules=["lin0", "my_module", "lin1"])
        config._register_custom_module({custom_module_cls: custom_lora_cls})

        torch.manual_seed(1)
        peft_model = get_peft_model(model, config)

        inputs = torch.randn(5, 10)
        outputs_before = peft_model(inputs)  # does not raise

        sd_before = peft_model.state_dict()
        peft_model.save_pretrained(tmp_path / "lora-custom-module")
        del model, peft_model

        torch.manual_seed(0)  # same seed for base model
        model = model_cls()

        # custom lora mapping is not persisted at the moment, so as a workaround this is needed
        config = LoraConfig.from_pretrained(tmp_path / "lora-custom-module")
        config._register_custom_module({custom_module_cls: custom_lora_cls})

        # different seed for adapter to ensure it is not identical just because of seed
        torch.manual_seed(123)
        peft_model = PeftModel.from_pretrained(model, tmp_path / "lora-custom-module", config=config)
        assert isinstance(peft_model.base_model.model.my_module, custom_lora_cls)
        assert isinstance(peft_model.base_model.model.my_module.base_layer, custom_module_cls)

        outputs_after = peft_model(inputs)  # does not raise
        assert torch.allclose(outputs_before, outputs_after)

        sd_after = peft_model.state_dict()
        assert sd_before.keys() == sd_after.keys()
        for key in sd_before.keys():
            assert torch.allclose(sd_before[key], sd_after[key])

    def test_override_lora_linear(self, custom_lora_cls):
        # in this test, we check if users can override default PEFT behavior by supplying a custom lora class that is
        # being used instead of lora.Linear
        model = AutoModelForCausalLM.from_pretrained("facebook/opt-125m")
        config = LoraConfig(task_type=TaskType.CAUSAL_LM)
        config._register_custom_module({nn.Linear: custom_lora_cls})
        peft_model = get_peft_model(model, config)
        layers = peft_model.base_model.model.model.decoder.layers
        for layer in layers:
            assert isinstance(layer.self_attn.v_proj, custom_lora_cls)
            assert isinstance(layer.self_attn.q_proj, custom_lora_cls)

    def test_custom_lora_layer_issues_warning(self, custom_module_cls, custom_lora_cls, model_cls, recwarn):
        # users will get a warning if they target a layer type that is not officially supported
        model = model_cls()
        config = LoraConfig(target_modules=["lin0", "my_module", "lin1"])
        config._register_custom_module({custom_module_cls: custom_lora_cls})

        get_peft_model(model, config)
        # check warning message
        msg = (
            "Unsupported layer type '<class 'tests.test_custom_models.TestDynamicDispatch.custom_module_cls."
            "<locals>.MyModule'>' encountered, proceed at your own risk."
        )
        assert str(recwarn.list[-1].message) == msg

    def test_target_layer_without_in_features_out_features(self, recwarn):
        # It should be possible for users to target layers even if we cannot determine in_features and out_features.
        # Those are only needed to initialize the LoRA layer via update_layer, so as long as users take care of that,
        # they should be good and not require those attributes to exist
        from peft.tuners import lora

        class MyModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.lstm = nn.LSTM(10, 20)

        class MyLora(nn.Module, lora.LoraLayer):
            def __init__(self, base_layer, adapter_name, **kwargs):
                super().__init__()
                lora.LoraLayer.__init__(self, base_layer, **kwargs)
                self._active_adapter = adapter_name

        model = MyModel()
        # check that in_features and out_features attributes don't exist on LSTM
        assert not hasattr(model.lstm, "in_features")
        assert not hasattr(model.lstm, "out_features")

        config = LoraConfig(target_modules=["lstm"])
        config._register_custom_module({nn.LSTM: MyLora})
        peft_model = get_peft_model(model, config)

        # check that custom LoRA layer is correctly applied
        assert isinstance(peft_model.base_model.lstm, MyLora)
        assert isinstance(peft_model.base_model.lstm.base_layer, nn.LSTM)

        # we should still get a warning message
        msg = "Unsupported layer type '<class 'torch.nn.modules.rnn.LSTM'>' encountered, proceed at your own risk."
        assert str(recwarn.list[-1].message) == msg
