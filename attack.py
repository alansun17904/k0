import os
import torch
import argparse
import numpy as np
from transformers import AutoTokenizer, AutoModelForSequenceClassification, pipeline

from textattack import Attacker, AttackArgs
import textattack.attack_recipes as recipes
from textattack.datasets import HuggingFaceDataset
from textattack.models.wrappers import HuggingFaceModelWrapper


parser = argparse.ArgumentParser()
parser.add_argument("split", type=str, help="train/test")
parser.add_argument("model_name", type=str, help="hugging face model repo id")
parser.add_argument("base_model_name", type=str, help="name of the base model")
parser.add_argument("check_point_dir", type=str, help="directory to store check points")
parser.add_argument("output_filename", type=str, help="name of the output file")
parser.add_argument(
    "-marking_style",
    type=str,
    help="marking style for adv perturbations",
    default="file",
)

options = parser.parse_args()

DATASET = "imdb"
MODEL_NAME = options.model_name
BASE_MODEL_NAME = options.base_model_name
CHECKPOINT_DIR = options.check_point_dir
RECIPE = recipes.TextFoolerJin2019

tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_NAME)
origin_model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)

if BASE_MODEL_NAME == "gpt2":
    tokenizer.pad_token = tokenizer.eos_token

model = HuggingFaceModelWrapper(origin_model, tokenizer)
dataset = HuggingFaceDataset(DATASET, None, options.split, shuffle=True)

attack_args = AttackArgs(
    num_examples=5000,
    # checkpoint_interval=1000,
    checkpoint_dir=CHECKPOINT_DIR,
    csv_coloring_style=options.marking_style,
    log_to_csv=f"{options.output_filename}.csv",
    query_budget=300,
    parallel=True,
    # num_workers_per_device=2,
)

if __name__ == "__main__":
    attacker = Attacker(RECIPE.build(model), dataset, attack_args)
    attacker.attack_dataset()
