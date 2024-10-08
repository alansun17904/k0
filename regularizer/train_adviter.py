import os
import torch
import evaluate
import pandas as pd
import torch.nn.functional as F
import numpy as np
import argparse
from datasets import load_dataset
from model import KnowledgeContinuousModel
from huggingface_hub import ModelCard, create_repo
from transformers import (
    AutoTokenizer,
    AutoModelForSequenceClassification,
    Trainer,
    TrainingArguments,
    TrainerCallback,
)


metric = evaluate.load("accuracy")


def prepare_dataset(dataset, tokenizer, is_gpt=False):
    if is_gpt:
        tokenizer.pad_token = tokenizer.eos_token
    def tokenize(batch):
        return tokenizer(batch["text"], padding=True, truncation=True)

    tokenized_dataset = dataset.map(tokenize, batched=True)
    return tokenized_dataset


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    predictions = np.argmax(logits, axis=-1)
    return metric.compute(predictions=predictions, references=labels)


def split_dataset(dataset):
    train_dataset, test_dataset = dataset["train"], dataset["test"]
    shuffled_test = test_dataset.shuffle(seed=42)
    valid_dataset, test_dataset = shuffled_test.select(
        range(10000)
    ), shuffled_test.select(range(10000, len(test_dataset)))
    return train_dataset, valid_dataset, test_dataset


# TODO: here we are not specifying a delta value, but we need to do this cause in the proofs we
# are doing this.
# TODO: also need to specify a lambda value on the regularizer term
class LoggingCallback(TrainerCallback):
    def on_epoch_end(self, args, state, control, **kwargs):
        content = f"""
---
language: en
license: mit
library_name: pytorch
---
# Adversarial Training Through Iterations
Trainer Hyperparameters:
- `lr` = {args.learning_rate}
- `per_device_batch_size` = {args.per_device_train_batch_size}
- `gradient_accumulation_steps` = {args.gradient_accumulation_steps}
- `weight_decay` = {args.weight_decay}
- `seed` = {args.seed}

Extended Logs:

|eval_loss|eval_accuracy|epoch|
|--|--|--|
"""
        for state_epoch in state.log_history:
            if "eval_loss" in state_epoch:
                content += f"|{state_epoch['eval_loss']:.3f}|{state_epoch['eval_accuracy']:.3f}|{state_epoch['epoch']}|\n"
        card = ModelCard(content)
        card.push_to_hub(f"asun17904/{args.hub_model_id}", token=os.environ["HUB_TOKEN"])


class KnowledgeRegularizedTrainer(Trainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def prediction_step(self, model, inputs, prediction_loss_only, ignore_keys=None):
        with torch.no_grad():
            labels = inputs.get("labels", None)
            prediction_loss, model_output = self.compute_loss(
                model, inputs, return_outputs=True
            )
            _, logits = model_output
            if prediction_loss_only:
                return (prediction_loss, None, None)
            return (prediction_loss, logits, labels)

    def calc_knowledge_discontinuities(self, class_losses, hs):
        dist = torch.cdist(hs, hs) + self.stabilizer
        loss_dist = torch.cdist(class_losses, class_losses, p=1)
        return torch.sum(loss_dist / dist)

    def compute_loss(self, model, inputs, return_outputs=False):
        labels = inputs.get("labels")
        outputs = model(**inputs)
        hs, logits = outputs
        logits = logits.softmax(dim=1)
        class_loss = F.cross_entropy(logits, labels)  # N x 1
        if return_outputs:
            return class_loss, outputs
        return class_loss
    

class ALUMTrainer(Trainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def adv_project(self, grad, norm_type='inf', eps=1e-6):
        if norm_type == 'l2':
            direction = grad / (torch.norm(grad, dim=-1, keepdim=True) + eps)
        elif norm_type == 'l1':
            direction = grad.sign()
        else:
            direction = grad / (grad.abs().max(-1, keepdim=True)[0] + eps)
        return direction
    
    @staticmethod
    def KL(input, target, reduction="sum"):
        input = input.float()
        target = target.float()
        loss = F.kl_div(F.log_softmax(input, dim=-1, dtype=torch.float32), F.softmax(target, dim=-1, dtype=torch.float32), reduction=reduction)
        return loss

    def prediction_step(self, model, inputs, prediction_loss_only, ignore_keys=None):
        with torch.no_grad():
            labels = inputs.get("labels", None)
            prediction_loss, logits = self.compute_loss(
                model, inputs, return_outputs=True, evaluation_c=True,
            )
            if prediction_loss_only:
                return (prediction_loss, None, None)
            return (prediction_loss, logits, labels)

    def compute_loss(self, model, inputs, return_outputs=False, evaluation_c=False):
        labels = inputs.get("labels")
        outputs = model(**inputs, determinisitc_idx=0)  # get the first embeddings and output
        hs, logits = outputs  
        class_loss = F.cross_entropy(logits.softmax(dim=1), labels)
        if evaluation_c:
            if return_outputs:
                return class_loss, logits
            return class_loss
        # find the optimal adversarial direction within the sub-word embedding space
        # move in a random direction first
        noise = torch.normal(torch.zeros(hs.shape), torch.ones(hs.shape)) * 1e-5
        noise.requires_grad_()
        noise = noise.to(hs.device)
        new_embedding = hs.detach() + noise

        # remove input_ids from the inputs dictionary
        _, adv_logits = model(**inputs, inputs_embeds=new_embedding)
        # compare the KL between the new logits and the old ones
        adv_loss = ALUMTrainer.KL(adv_logits, logits.detach(), reduction="batchmean")
        # find the gradient with respect to the random perturbation
        delta_grad, = torch.autograd.grad(adv_loss, noise, only_inputs=True)
        delta_norm = delta_grad.norm()
        # normalize the gradient, then move in that direction
        # skip this if the norm of the gradient is too large
        if torch.isnan(delta_norm) or torch.isinf(delta_norm): 
            if return_outputs:
                return class_loss, logits
            return class_loss
        noise = noise + delta_grad * 1e-3
        # perform the projection again and find the loss
        noise = self.adv_project(noise, eps=1e-5)
        new_embedding = hs.detach() + noise
        _, adv_logits = model(**inputs, inputs_embeds=new_embedding)
        # find the symmetric KL loss
        adv_loss_f = KnowledgeRegularizedTrainer.KL(adv_logits, logits.detach())
        adv_loss_b = KnowledgeRegularizedTrainer.KL(logits, adv_logits.detach())
        adv_loss = 1e-3 * (adv_loss_f + adv_loss_b)
        
        if return_outputs:
            return class_loss + adv_loss, logits
        return class_loss + adv_loss


def prepare_trainer(
    base_model_name,
    model,
    train_dataset,
    valid_dataset,
    learning_rate,
    weight_decay,
    epochs=20,
):
    training_args = TrainingArguments(
        output_dir="imdb-kd-regularized",
        per_device_train_batch_size=8,
        gradient_accumulation_steps=2,
        learning_rate=learning_rate,
        num_train_epochs=epochs,
        evaluation_strategy="epoch",
        # eval_accumulation_steps=4,
        weight_decay=weight_decay,
        hub_token=os.environ.get("HUB_TOKEN"),
        hub_model_id=f"{base_model_name}-adviter",
        push_to_hub=True,
        save_steps=2000,
        seed=42,
    )
    trainer = KnowledgeRegularizedTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=valid_dataset,
        compute_metrics=compute_metrics,
        callbacks=[LoggingCallback()],
    )
    return trainer


parser = argparse.ArgumentParser()
parser.add_argument("model", type=str, help="name of the model (huggingface repo)")
parser.add_argument("base_model", type=str, help="name of the base model")
parser.add_argument("learning_rate", type=float, help="learning rate")
parser.add_argument("weight_decay", type=float, help="weight decay")
parser.add_argument("advtrain_file", type=str, help="csv file of adversarial examples")
parser.add_argument("-epochs", type=int, help="the number of training epochs")
parser.add_argument("-is_ed", type=bool, help="if the model is an encoder-decoder")

options = parser.parse_args()

tokenizer = AutoTokenizer.from_pretrained(options.base_model)
pretrained_model = AutoModelForSequenceClassification.from_pretrained(options.model)

# set the padding token if the model is gpt
if options.base_model == "gpt2":
    tokenizer.pad_token = tokenizer.eos_token
    pretrained_model.config.pad_token_id = pretrained_model.config.eos_token_id

dataset = load_dataset("imdb")
train_dataset, valid_dataset, test_dataset = split_dataset(dataset)
train_dataset = prepare_dataset(train_dataset, tokenizer, is_gpt=options.model=="gpt2")
valid_dataset = prepare_dataset(valid_dataset, tokenizer, is_gpt=options.model=="gpt2")


# get all of the successful adversarial attacks from the csv file
advtrain_df = pd.read_csv(options.advtrain_file)
advtrain_df = advtrain_df[advtrain_df["result_type"] == "Successful"]
advtest, advlabels = advtrain_df.perturbed_text, advtrain_df.ground_truth_output
# add these perturbed examples into the training dataset
for i in range(len(advtest)):
    train_dataset.add_item({
        "text": advtest.iloc[i],
        "label": advlabels.iloc[i]
    })

trainer = prepare_trainer(
    options.base_model,
    KnowledgeContinuousModel(
        pretrained_model,
        1,
        1,
        options.is_ed,
    ),
    train_dataset,
    valid_dataset,
    options.learning_rate,
    options.weight_decay,
    epochs=options.epochs,
)
# trainer.evaluate()
trainer.train()
# regularized_model = trainer.model.model
# push this to hub too
# regularized_model.save_pretrained(f"imdb-{options.model}-kd-regularized-base-l2")
