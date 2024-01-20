import os
import torch
import torch.nn.functional as F
import numpy as np
import argparse
from datasets import load_dataset
from model import KnowledgeContinuousModel
from transformers import AutoTokenizer, AutoModelForSequenceClassification, Trainer, TrainingArguments


def prepare_dataset(dataset, tokenizer):
    def tokenize(batch):
        return tokenizer(batch["text"], padding=True, truncation=True)
    tokenized_dataset = dataset.map(tokenize, batched=True)
    return tokenized_dataset


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    predictions = np.argmax(logits, axis=-1)
    return {
        "accuracy": np.sum((predictions * labels)) / len(labels)
    }

def split_dataset(dataset):
    train_dataset, test_dataset = dataset["train"], dataset["test"]
    shuffled_test = test_dataset.shuffle(seed=42)
    valid_dataset, test_dataset = shuffled_test.select(range(10000)), shuffled_test.select(range(10000, len(test_dataset)))
    return train_dataset, valid_dataset, test_dataset


# TODO: here we are not specifying a delta value, but we need to do this cause in the proofs we
# are doing this.
# TODO: also need to specify a lambda value on the regularizer term
class KnowledgeRegularizedTrainer(Trainer):
    # def prediction_step(self, model, inputs, prediction_loss_only, ignore_keys=None):
    #     labels = inputs.get("labels", None)
    #     if prediction_loss_only:
    #         return (self.compute_loss(model, inputs), None, None)
    #     logits, hs = model(**inputs)
    #     return (None, logits, labels)

    def calc_knowledge_discontinuities(self, class_losses, hs):
        dist = torch.cdist(hs,hs) + 1e-2
        loss_dist = torch.cdist(class_losses, class_losses, p=1)
        return torch.sum(loss_dist / dist)

    def compute_loss(self, model, inputs, return_outputs=False):
        labels = inputs.get("labels")
        outputs = model(**inputs)
        hs, logits = outputs
        # labels = F.one_hot(labels,num_classes=2).float()
        # print(labels, logits)
        logits = logits.softmax(dim=1)
        class_loss = F.cross_entropy(logits, labels, reduction="none")  # N x 1
        # print(class_loss)
        # print(hs.shape)
        # kd_score = self.calc_knowledge_discontinuities(class_loss, hs)
        if return_outputs:
            return torch.sum(class_loss), outputs#  + 0 * kd_score, outputs
        return torch.sum(class_loss)#  + 0 * kd_score

def prepare_trainer(model, train_dataset, valid_dataset, epochs=20):
    training_args = TrainingArguments(
        output_dir="imdb-kd-regularized",
        learning_rate=5e-5,
        num_train_epochs=epochs,
        evaluation_strategy="epoch",
        hub_token=os.environ.get("HUB_TOKEN"),
        hub_model_id=f"imdb-kd-regularized",
        push_to_hub=True,
        save_steps=2000,
        seed=42
    )
    trainer = KnowledgeRegularizedTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=valid_dataset,
        compute_metrics=compute_metrics
    )
    return trainer


parser = argparse.ArgumentParser()
parser.add_argument("dataset", type=str, help="any dataset that is in the huggingface dataset module")
parser.add_argument("model", type=str, help="name of the model (huggingface repo)")
parser.add_argument("alpha", type=float, help="parameter in the beta distribution for choosing hidden layer")
parser.add_argument("beta", type=float, help="parameter in the beta distribution for choosing the hidden layer")
parser.add_argument("-epochs", type=int, help="the number of training epochs")

options = parser.parse_args()

tokenizer = AutoTokenizer.from_pretrained(options.model)
model = AutoModelForSequenceClassification.from_pretrained(options.model)
for name, W in model.named_parameters():
    if not W.requires_grad:
        print(f"gradient not enabled in layer: {name}")
print(model.bert.encoder.layer[0].attention)
print(model.bert.encoder.layer[0].attention.self.query.weight.requires_grad)
dataset = load_dataset(options.dataset)
train_dataset, valid_dataset, test_dataset = split_dataset(dataset)
train_dataset = prepare_dataset(train_dataset, tokenizer)
valid_dataset = prepare_dataset(valid_dataset, tokenizer)
trainer = prepare_trainer(
    KnowledgeContinuousModel(model, options.alpha, options.beta),
    train_dataset,
    valid_dataset,
    epochs=options.epochs
)
trainer.train()
regularized_model = trainer.model.model
# push this to hub too
regularized_model.push_to_hub(f"imdb-kd-regularized-base")



