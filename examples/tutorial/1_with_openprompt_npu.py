"""
This tutorial is a copy of OpenPrompt's tutorial/1.1_mixed_template.py
The only modification is in lines 98 to 102

1. OpenPrompt provides pre-processing of data, such as prompt template formatting
2. OpenPrompt pre-process the model input, such as: prompt soft embedding
3. OpenDelta modify the backbone model, such as: Adapter, Lora, Compactor, etc.
4. OpenPrompt post-process the model output, such as: extract logits at <mask> position, apply prompt verbalizer
"""

# load dataset
from datasets import load_dataset
from datasets import load_from_disk
raw_dataset = load_dataset('super_glue', 'cb', 
                        #    cache_dir="../datasets/.cache/huggingface_datasets"
                           )
# raw_dataset = load_from_disk("/home/hx/huggingface_datasets/saved_to_disk/super_glue.cb")
# Note that if you are running this scripts inside a GPU cluster, there are chances are you are not able to connect to huggingface website directly. 
# In this case, we recommend you to run `raw_dataset = load_dataset(...)` on some machine that have internet connections. 
# Then use `raw_dataset.save_to_disk(path)` method to save to local path.
# Thirdly upload the saved content into the machiine in cluster. 
# Then use `load_from_disk` method to load the dataset. 

from openprompt.data_utils import InputExample

dataset = {}
for split in ['train', 'validation', 'test']:
    dataset[split] = []
    for data in raw_dataset[split]:
        input_example = InputExample(text_a = data['premise'], text_b = data['hypothesis'], label=int(data['label']), guid=data['idx'])
        dataset[split].append(input_example)
print(dataset['train'][0])

# You can load the plm related things provided by openprompt simply by calling:
from openprompt.plms import load_plm
plm, tokenizer, model_config, WrapperClass = load_plm("t5", "t5-base")

# Constructing Template
# A template can be constructed from the yaml config, but it can also be constructed by directly passing arguments.
from openprompt.prompts import MixedTemplate
template_text = '{"placeholder":"text_a"} {"soft"} {"soft"} {"soft"} {"placeholder":"text_b"}? {"soft"} {"soft"} {"soft"} {"mask"}.'
mytemplate = MixedTemplate(model=plm, tokenizer=tokenizer, text=template_text)

# To better understand how does the template wrap the example, we visualize one instance.

wrapped_example = mytemplate.wrap_one_example(dataset['train'][0]) 
print(wrapped_example)

# Now, the wrapped example is ready to be pass into the tokenizer, hence producing the input for language models.
# You can use the tokenizer to tokenize the input by yourself, but we recommend using our wrapped tokenizer, which is a wrapped tokenizer tailed for InputExample. 
# The wrapper has been given if you use our `load_plm` function, otherwise, you should choose the suitable wrapper based on
# the configuration in `openprompt.plms.__init__.py`.
# Note that when t5 is used for classification, we only need to pass <pad> <extra_id_0> <eos> to decoder.
# The loss is calcaluted at <extra_id_0>. Thus passing decoder_max_length=3 saves the space
wrapped_t5tokenizer = WrapperClass(max_seq_length=128, decoder_max_length=3, tokenizer=tokenizer,truncate_method="head")
# or
from openprompt.plms import T5TokenizerWrapper
wrapped_t5tokenizer= T5TokenizerWrapper(max_seq_length=128, decoder_max_length=3, tokenizer=tokenizer,truncate_method="head")

# You can see what a tokenized example looks like by 
tokenized_example = wrapped_t5tokenizer.tokenize_one_example(wrapped_example, teacher_forcing=False)
print(tokenized_example)
print(tokenizer.convert_ids_to_tokens(tokenized_example['input_ids']))
print(tokenizer.convert_ids_to_tokens(tokenized_example['decoder_input_ids']))

# Now it's time to convert the whole dataset into the input format!
# Simply loop over the dataset to achieve it!

model_inputs = {}
for split in ['train', 'validation', 'test']:
    model_inputs[split] = []
    for sample in dataset[split]:
        tokenized_example = wrapped_t5tokenizer.tokenize_one_example(mytemplate.wrap_one_example(sample), teacher_forcing=False)
        model_inputs[split].append(tokenized_example)


# We provide a `PromptDataLoader` class to help you do all the above matters and wrap them into an `torch.DataLoader` style iterator.
from openprompt import PromptDataLoader

train_dataloader = PromptDataLoader(dataset=dataset["train"], template=mytemplate, tokenizer=tokenizer, 
    tokenizer_wrapper_class=WrapperClass, max_seq_length=256, decoder_max_length=3, 
    batch_size=4,shuffle=True, teacher_forcing=False, predict_eos_token=False,
    truncate_method="head")


# Define the verbalizer
# In classification, you need to define your verbalizer, which is a mapping from logits on the vocabulary to the final label probability. Let's have a look at the verbalizer details:

from openprompt.prompts import ManualVerbalizer
import torch

# for example the verbalizer contains multiple label words in each class
myverbalizer = ManualVerbalizer(tokenizer, num_classes=3, label_words=[["yes"], ["no"], ["maybe"]])

print("label_words_ids", myverbalizer.label_words_ids)

# Although you can manually combine the plm, template, verbalizer together, we provide a pipeline 
# model which take the batched data from the PromptDataLoader and produce a class-wise logits

from opendelta import LoraModel
# delta_model = LoraModel(backbone_model=plm, modified_modules=[])
delta_model = LoraModel(backbone_model=plm, modified_modules=["SelfAttention.q", "SelfAttention.v"])
delta_model.freeze_module(exclude=["deltas"], set_state_dict=True)
delta_model.log()

from openprompt import PromptForClassification

use_npu = True
prompt_model = PromptForClassification(plm=plm, template=mytemplate, verbalizer=myverbalizer)
if use_npu :
    prompt_model = prompt_model.npu()

# Now the training is standard
from transformers import  AdamW, get_linear_schedule_with_warmup
loss_func = torch.nn.CrossEntropyLoss()
no_decay = ['bias', 'LayerNorm.weight']
# it's always good practice to set no decay to biase and LayerNorm parameters
optimizer_grouped_parameters = [
    {'params': [p for n, p in prompt_model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
    {'params': [p for n, p in prompt_model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
]
print([n for n, p in prompt_model.named_parameters()])

optimizer = AdamW(optimizer_grouped_parameters, lr=1e-4)

for epoch in range(30):
    tot_loss = 0 
    for step, inputs in enumerate(train_dataloader):
        if use_npu:
            # The inputs instance is of type InputFeature, which inherits from dict. 
            # The to() method can move it to other devices. The cuda() method is a wrapper for to(), specifically moving to CUDA devices. 
            # If you want to move it to an NPU device, you can directly use the underlying to() method.
            inputs = inputs.to("npu")
            delta_model.log()
        logits = prompt_model(inputs)
        labels = inputs['label']
        loss = loss_func(logits, labels)
        loss.backward()
        tot_loss += loss.item()
        optimizer.step()
        optimizer.zero_grad()
        if step %100 ==1:
            print("Epoch {}, average loss: {}".format(epoch, tot_loss/(step+1)), flush=True)
    
# Evaluate
validation_dataloader = PromptDataLoader(dataset=dataset["validation"], template=mytemplate, tokenizer=tokenizer, 
    tokenizer_wrapper_class=WrapperClass, max_seq_length=256, decoder_max_length=3, 
    batch_size=4,shuffle=False, teacher_forcing=False, predict_eos_token=False,
    truncate_method="head")

allpreds = []
alllabels = []
for step, inputs in enumerate(validation_dataloader):
    if use_npu:
        inputs = inputs.to("npu")
    logits = prompt_model(inputs)
    labels = inputs['label']
    alllabels.extend(labels.cpu().tolist())
    allpreds.extend(torch.argmax(logits, dim=-1).cpu().tolist())

acc = sum([int(i==j) for i,j in zip(allpreds, alllabels)])/len(allpreds)
print(acc)