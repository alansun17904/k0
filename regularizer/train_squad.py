from huggingface_hub import notebook_login
from datasets import load_dataset
from transformers import AutoTokenizer, AutoModelForCausalLM, TrainingArguments, Trainer
import math

dataset = load_dataset("squad")
dataset

model_checkpoint = 'gpt2'
tokenizer = AutoTokenizer.from_pretrained(model_checkpoint, use_fast=True)

special_tokens = tokenizer.special_tokens_map
print(special_tokens)


def add_end_token_to_question(input_dict):
    input_dict['question'] += special_tokens['bos_token']
    return input_dict

dataset = dataset.remove_columns(['id', 'title', 'context', 'answers'])
dataset = dataset.map(add_end_token_to_question)


def tokenize_function(input_dict):
    return tokenizer(input_dict['question'], truncation=True)
tokenized_dataset = dataset.map(tokenize_function, batched=True, num_proc=4, remove_columns=['question'])
tokenized_dataset


max_block_length = 128

def divide_tokenized_text(tokenized_text_dict, block_size):
    """
    Divides the tokenized text in the examples into fixed-length blocks of size block_size.

    Parameters:
    -----------
    tokenized_text_dict: dict
        A dictionary containing tokenized text as values for different keys.

    block_size: int
        The desired length of each tokenized block.

    Returns:
    -----------
        dict: A dictionary with tokenized text divided into fixed-length blocks.
    """
    concatenated_examples = {k: sum(tokenized_text_dict[k], []) for k in tokenized_text_dict.keys()}
    total_length = len(concatenated_examples[list(tokenized_text_dict.keys())[0]])
    total_length = (total_length // block_size) * block_size

    result = {
        k: [t[i: i + block_size] for i in range(0, total_length, block_size)]
        for k, t in concatenated_examples.items()
    }

    result['labels'] = result['input_ids'].copy()
    return result


lm_dataset = tokenized_dataset.map(
    lambda tokenized_text_dict: divide_tokenized_text(tokenized_text_dict, max_block_length),
    batched=True,
    batch_size=1000,
    num_proc=4,
)

train_dataset = lm_dataset['train'].shuffle(seed=42)
eval_dataset = lm_dataset['validation'].shuffle(seed=42)

model = AutoModelForCausalLM.from_pretrained("gpt2")
tokenizer.add_special_tokens({'pad_token': '[PAD]'})


training_args = TrainingArguments(
    output_dir="imdb-kd-regularized",
    per_device_train_batch_size=8,
    gradient_accumulation_steps=2,
    learning_rate=5e-5,
    num_train_epochs=3,
    evaluation_strategy="epoch",
    # eval_accumulation_steps=4,
    weight_decay=1e-9,
    hub_token=os.environ.get("HUB_TOKEN"),
    hub_model_id=f"gpt2-squad",
    push_to_hub=True,
    save_steps=2000,
    seed=42,
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=eval_dataset,
    tokenizer=tokenizer,
)

eval_results = trainer.evaluate()
print(f'Perplexity: {math.exp(eval_results["eval_loss"]):.2f}')