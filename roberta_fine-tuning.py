import argparse
import glob
import logging
import os
import random
import timeit
import itertools

import numpy as np

from tqdm import tqdm, trange
from torch.utils.data import Dataset, DataLoader, RandomSampler, SequentialSampler, BatchSampler, Sampler
import sys
import mutils as mt
# sys.path.append('./../')
from utils.util import *
from qa_hf.predict import improve_prediction

from transformers import (
    AutoConfig,
    AutoTokenizer,
    AutoModelForQuestionAnswering,
    get_scheduler
)
from torch.optim import AdamW
from datasets import load_dataset

# Injected code to modify the Unqover config.json.
from huggingface_hub import hf_hub_download
import shutil
from transformers.models.roberta import RobertaTokenizerFast

'''
from transformers import (
    AdamW,
    WEIGHTS_NAME,
    AutoConfig,
    get_linear_schedule_with_warmup,
    squad_convert_examples_to_features,
)
'''

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter

logger = logging.getLogger(__name__)

def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

class ConcatDataset(Dataset):
    "concat different datasets"
    def __init__(self, datasets):
        self.datasets = datasets

    def __getitem__(self, index):
        len_of_datasets = [len(dt) for dt in self.datasets]
        for i in range(0, len(len_of_datasets)):
            if index < len_of_datasets[i]:
                return self.datasets[i][index]
            else:
                index -= len_of_datasets[i]
    def __len__(self):
        return sum([len(dt) for dt in self.datasets])


def train(args, model, tokenizer):
    """ Train the model """
    tb_writer = SummaryWriter('tb_writer')

    # Get the data directory in relation to the cwd.
    unqover_train, _, _, _, _, _, _  = mt.get_data(os.path.join(f"{os.getcwd()}\\{args.data_dir}", f'addedbias_train_{args.num_train}_squad.json'), tokenizer, count=-1,  replace2token=args.replace2token)
    # unqover_train, _, _, _, _, _, _  = mt.get_data(os.path.join(args.data_dir, f'addedbias_train_{args.num_train}_squad.json'), tokenizer, count=args.num_train*3//4,  replace2token=args.replace2token)
    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    idx0 = list(range(len(unqover_train)))
    random.shuffle(idx0)

    if args.squad:
        logger.info("adding squad data")
        squadcount = min(int(args.num_train //4 * 7), 100000)
        squad_encoding_all = mt.get_squad_data('./inputs/squad/train-v1.1.json', tokenizer, count= squadcount)
        indices = random.sample(range(squadcount), args.num_train//4)
        squad_encoding = {}
        for key in squad_encoding_all:
            squad_encoding[key] = [squad_encoding_all[key][x] for x in indices]
        squad_train = mt.SquadFormat(squad_encoding)
        train_dataset = ConcatDataset([unqover_train, squad_train])
        idx1 = list(range(len(unqover_train), len(train_dataset)))
        random.shuffle(idx1)
    else:
        logger.info("no squad data")
        train_dataset = unqover_train

    print("data size:", len(train_dataset))
    assert len(train_dataset) == args.num_train
    indices = []
    if args.squad:
        for i in range(args.num_train // args.train_batch_size):
            indices.extend(idx0[i * args.train_batch_size *3//4: (i+ 1)* args.train_batch_size*3//4])
            indices.extend(idx1[i * args.train_batch_size//4: (i+1)* args.train_batch_size//4])
    else:
        for i in range(len(idx0) // args.train_batch_size ):
            indices.extend(idx0[i * args.train_batch_size: (i + 1) * args.train_batch_size])
        if len(idx0) % args.train_batch_size != 0:
            indices.extend(idx0[(len(idx0) // args.train_batch_size )*args.train_batch_size: ])

    # print("qestion token positions:", train_dataset.encodings['start_positions'][:5])
    # print("qestion token end positions:", train_dataset.encodings['end_positions'][:5])
    # print(tokenizer.convert_ids_to_tokens(train_dataset.encodings['input_ids'][0]))
    batchspl = BatchSampler(indices, batch_size = args.train_batch_size, drop_last=False)
    
    train_dataloader = DataLoader(train_dataset, batch_sampler = batchspl) #now in each batch, half is unqover dataset, half is squad dataset

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0},
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_scheduler(
        "linear", optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total
    )

    # Check if saved optimizer or scheduler states exist
    if os.path.isfile(os.path.join(args.model_name_or_path, "optimizer.pt")) and os.path.isfile(
        os.path.join(args.model_name_or_path, "scheduler.pt")
    ):
        # Load in optimizer and scheduler states
        optimizer.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "optimizer.pt")))
        scheduler.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "scheduler.pt")))


    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)


    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info(
        "  Total train batch size (w. parallel, distributed & accumulation) = %d",
        args.train_batch_size
        * args.gradient_accumulation_steps
    )
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 1
    epochs_trained = 0
    steps_trained_in_current_epoch = 0
    # Check if continuing training from a checkpoint
    if os.path.exists(args.model_name_or_path):
        try:
            # set global_step to gobal_step of last saved checkpoint from model path
            checkpoint_suffix = args.model_name_or_path.split("-")[-1].split("/")[0]
            global_step = int(checkpoint_suffix)
            epochs_trained = global_step // (len(train_dataloader) // args.gradient_accumulation_steps)
            steps_trained_in_current_epoch = global_step % (len(train_dataloader) // args.gradient_accumulation_steps)

            logger.info("  Continuing training from checkpoint, will skip to saved global_step")
            logger.info("  Continuing training from epoch %d", epochs_trained)
            logger.info("  Continuing training from global step %d", global_step)
            logger.info("  Will skip the first %d steps in the first epoch", steps_trained_in_current_epoch)
        except ValueError:
            logger.info("  Starting fine-tuning.")

    tr_loss, logging_loss = 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(
        epochs_trained, int(args.num_train_epochs), desc="Epoch" 
    )
    # Added here for reproductibility
    set_seed(args)

    for _ in train_iterator:
        if args.squad:
            indices = random.sample(range(squadcount), args.num_train // 4)
            squad_encoding = {}
            for key in squad_encoding_all:
                squad_encoding[key] = [squad_encoding_all[key][x] for x in indices]
            squad_train = mt.SquadFormat(squad_encoding)
            train_dataset = ConcatDataset([unqover_train, squad_train])
            train_dataloader = DataLoader(train_dataset, batch_sampler = batchspl) #now in each batch, half is unqover dataset, half is squad dataset

        epoch_iterator = tqdm(train_dataloader, desc="Iteration")
        for step, batch in enumerate(epoch_iterator):
            # Skip past any already trained steps if resuming training
            if steps_trained_in_current_epoch > 0:
                steps_trained_in_current_epoch -= 1
                continue

            model.train()
            # print("batch:", batch)
            input_ids = batch['input_ids'].to(args.device)
            attention_mask = batch['attention_mask'].to(args.device)
            # print("attention mask:", attention_mask)
            start_positions = batch['start_positions'].to(args.device)
            end_positions = batch['end_positions'].to(args.device)
            interventions = batch['interventions'].to(args.device)
            start_scores = batch["start_scores"].to(args.device)
            end_scores = batch["end_scores"].to(args.device)
            tomax = batch["tomax"].to(args.device)

            considered = torch.where(interventions != -2)
            if len(considered[0]) == 0:
                continue
            outputs = model(input_ids, attention_mask=attention_mask, start_positions=start_positions, end_positions=end_positions, interventions=interventions, start_scores = start_scores, end_scores = end_scores, tomax = tomax, doadversarial=True, doirrelevant = args.doirrelevant, args = args) 
            # outputs = model(input_ids[considered], attention_mask=attention_mask[considered], start_positions=start_positions[considered], end_positions=end_positions[considered], interventions=interventions[considered], start_scores=start_scores[considered], end_scores=end_scores[considered],tomax = tomax[considered], doadversarial=True, doirrelevant = args.doirrelevant) 
           

            # model outputs are always tuple in transformers (see doc)
            loss = outputs[0]

            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel (not distributed) training
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            
            loss.backward()

            epoch_iterator.set_postfix(loss=loss.item())
            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

                # Log metrics
                if args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    # Only evaluate when single GPU otherwise metrics may not average well
                    if args.evaluate_during_training:
                        results = evaluate(args, model, tokenizer)
                        tb_writer.add_scalar("eval_loss", np.average(results), global_step)
                    tb_writer.add_scalar("lr", scheduler.get_lr()[0], global_step)
                    tb_writer.add_scalar("loss", (tr_loss - logging_loss) / args.logging_steps, global_step)
                    logging_loss = tr_loss

                # Save model checkpoint
                if args.save_steps > 0 and global_step % args.save_steps == 0:
                    output_dir = os.path.join(args.output_dir, "checkpoint-{}".format(global_step))
                    print("******checkpoint dir******:", output_dir)
                    # Take care of distributed/parallel training
                    model_to_save = model.module if hasattr(model, "module") else model
                    model_to_save.save_pretrained(output_dir)
                    tokenizer.save_pretrained(output_dir)

                    torch.save(args, os.path.join(output_dir, "training_args.bin"))
                    logger.info("Saving model checkpoint to %s", output_dir)

                    torch.save(optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
                    torch.save(scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
                    logger.info("Saving optimizer and scheduler states to %s", output_dir)

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break
        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    tb_writer.close()

    return global_step, tr_loss / global_step



def evaluate(args, model, tokenizer, prefix=""):

    dataset, all_tokens, all_ids, all_keys, all_contexts, all_questions, all_answers = mt.get_data(os.path.join(args.data_dir, f'addedbias_noniid_{args.evaldev}_squad.json'), tokenizer, count=-1, replace2token=args.replace2token)
    # dataset, all_tokens, all_ids, all_keys, all_contexts, all_questions, all_answers = mt.get_data(os.path.join(args.data_dir, f'addedbias_train_20_squad.json'), tokenizer, count=-1, replace2token=args.replace2token)
    # exit()
    assert len(dataset) == len(all_ids)
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)

    # Note that DistributedSampler samples randomly
    eval_sampler = SequentialSampler(dataset)
    eval_dataloader = DataLoader(dataset, shuffle=False, batch_size=args.eval_batch_size)

    # multi-gpu evaluate
    if args.n_gpu > 1 and not isinstance(model, torch.nn.DataParallel):
        model = torch.nn.DataParallel(model)

    # Eval!
    logger.info("***** Running evaluation {} *****".format(prefix))
    logger.info("  Num examples = %d", len(dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)

    all_results = []
    start_time = timeit.default_timer()
    cur_cnt = 0
    rs_map = {}
    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        model.eval()
        # print("batch:", batch)
        input_ids = batch['input_ids'].to(args.device)
        attention_mask = batch['attention_mask'].to(args.device)
        start_positions = batch['start_positions'].to(args.device)
        end_positions = batch['end_positions'].to(args.device)
        interventions = batch['interventions'].to(args.device)
        start_scores = batch["start_scores"].to(args.device)
        end_scores = batch["end_scores"].to(args.device)
        tomax = batch["tomax"].to(args.device)

        non_irrelevant = torch.where(interventions != -2)

        if len(non_irrelevant[0]) < 1: #now skip the irrelevant examples
            cur_cnt += len(input_ids)
            continue
        
       

        with torch.no_grad():
            outputs = model(input_ids[non_irrelevant], attention_mask=attention_mask[non_irrelevant], start_positions=start_positions[non_irrelevant], \
                end_positions=end_positions[non_irrelevant], interventions=interventions[non_irrelevant],\
                    start_scores = start_scores[non_irrelevant], end_scores = end_scores[non_irrelevant], tomax = tomax, doadversarial = True, doirrelevant = True, args = args)

            non_irrelevant = non_irrelevant[0].cpu().numpy()
            # print("non_irrelevant:", non_irrelevant)
            
            assert len(non_irrelevant) == len(input_ids)

            batch_keys = list(map(all_keys[cur_cnt: cur_cnt + len(input_ids)].__getitem__, non_irrelevant))
            batch_ids = list(map(all_ids[cur_cnt: cur_cnt + len(input_ids)].__getitem__, non_irrelevant))
            batch_contexts = list(map(all_contexts[cur_cnt: cur_cnt + len(input_ids)].__getitem__, non_irrelevant))
            batch_tokens = list(map(all_tokens[cur_cnt: cur_cnt + len(input_ids)].__getitem__, non_irrelevant))
            batch_questions = list(map(all_questions[cur_cnt: cur_cnt + len(input_ids)].__getitem__, non_irrelevant))
            batch_answers = list(map(all_answers[cur_cnt: cur_cnt + len(input_ids)].__getitem__, non_irrelevant))

            # print("batch_answers:", batch_answers)
            # print("start pos:",batch['start_positions'][non_irrelevant])
            # print("end pos:",batch['end_positions'][non_irrelevant])

            loss, start_logits, end_logits = outputs
            # all_results.append([loss, starrt_logits, end_logits])
            if args.n_gpu > 1:
                loss = loss.mean()
            all_results.append(loss.item())

            # print("start logits:", start_logits)

            '''start unqover bias calcuation'''
            p_start, p_end = torch.nn.functional.softmax(start_logits, dim=-1), torch.nn.functional.softmax(end_logits, dim=-1)
            # print("p_start:", p_start)

            pred_span = pick_best_span_bounded(p_start.data.cpu().float(), p_end.data.cpu().float(), args.span_l)

            # print("pred_span:", pred_span)
            # print("pred_span.shape:", pred_span.shape)
            # print("batch_keys.shape:", len(batch_keys))
            # print(batch_tokens)


            for k in range(pred_span.shape[0]):
                keys = 'None|None|' + batch_keys[k]
                if 'irrelevant' in keys:
                    pass
                    # print(cur_cnt, len(input_ids))
                    # print(all_keys[cur_cnt: cur_cnt + len(input_ids)])
                    # print(batch_keys)
                    # print(interventions)
                    # print(non_irrelevant)
                if keys not in rs_map:
                    rs_map[keys] = [] 
                tmp = {}
                tmp['line'] = batch_ids[k][:-1] 
                tmp['context'] = batch_contexts[k]


                q_row = {}
                q_row['question'] = batch_questions[k]

                pred_ans = ' '.join(batch_tokens[k][pred_span[k][0]: pred_span[k][1]+1])
                q_row['pred'] = cleanup_G(pred_ans)

                # print(batch['start_positions'][non_irrelevant][k])


                for z, span in enumerate(zip(batch['start_positions'][non_irrelevant][k], batch['end_positions'][non_irrelevant][k])):
                    key = 'ans{0}'.format(z)

                    # better coverage on article, quantifiers, and etc.
                    s, e = improve_prediction('roberta', batch_tokens[k], p_start[k], p_end[k], span[0], span[1]) #s: start logit

                    q_row[key] = {'text': batch_answers[k][z]['text'], 'start': s, 'end': e}

                tmp['q{0}'.format(batch_ids[k][-1])] = q_row
                rs_map[keys].append(tmp)

            cur_cnt += len(interventions)

    assert cur_cnt == len(dataset)
    evalTime = timeit.default_timer() - start_time
    logger.info("  Evaluation done in total %f secs (%f sec per example)", evalTime, evalTime / len(dataset))
    logger.info(f"dev_loss:{np.mean(all_results)}")
		# batch_cnt += batch.size()[0]
# 
		# if batch_cnt % 1000 == 0:
			# print("predicted {} examples".format(batch_cnt))
# 
	# print('predicted {0} examples'.format(batch_cnt))

	# organize a bit
    ls = []
    for keys, ex in rs_map.items():
        toks = keys.split('|') 
        sort_keys = sorted(toks[0:3])
        sort_keys.extend(toks[3:])
        sort_keys = '|'.join(sort_keys)
        ls.append((sort_keys, keys, ex))
    ls = sorted(ls, key=lambda x: x[0])
    rs_map = {keys:ex for sort_keys, keys, ex in ls}
    if prefix != "":
        output_prediction_file = os.path.join(args.output_dir, "{}_{}-{}-{}.output.json".format(args.model_type, args.category,args.evaldev, prefix))
    else:
        output_prediction_file = os.path.join(args.output_dir, "{}_{}-{}.output.json".format(args.model_type, args.category, args.evaldev))
    print(f"save {len(ls)} examples to {output_prediction_file}")
    json.dump(rs_map, open(output_prediction_file, 'w'), indent=4)
    return all_results




def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--model_type", default='roberta', type=str, required=True, help="Model type such as bert, roberta.")
    parser.add_argument("--model_name_or_path", default='roberta-base', type=str, required=True, help="Path to pretrained model or model identifier from huggingface.co/models",)
    parser.add_argument(
        "--output_dir", default='output', type=str, required=True, help="The output directory where the model checkpoints and predictions will be written.",
    )

    # Other parameters
    parser.add_argument(
        "--data_dir", default='.', type=str, help="The input data dir. Should contain the .json files for the task."
        + "If no data dir or train/predict files are specified, will run with tensorflow_datasets.",
    )
    parser.add_argument(
        "--max_seq_length", default=384, type=int, help="The maximum total input sequence length after WordPiece tokenization. Sequences "
        "longer than this will be truncated, and sequences shorter than this will be padded.",
    )
    parser.add_argument(
        "--overwrite_output_dir", action="store_true", help="Overwrite the content of the output directory"
    )
    parser.add_argument("--category", default="religion", help="which category of unqover.")
    parser.add_argument("--train", action="store_true", help="Whether to run training.")
    parser.add_argument("--squad", action="store_true", help="Whether to fine-tuning with squad data.")
    parser.add_argument("--doirrelevant", action="store_true", help="Whether to fine-tuning with irrelevant interventions.")
    parser.add_argument("--doadversarial", action="store_true", help="Whether to fine-tuning with adversarial interventions.")
    parser.add_argument("--eval", action="store_true", help="Whether to run eval on the dev set.")
    parser.add_argument("--evaldev", default="dev", help="which test set to eval the model. For religion, we have [dev, test]. For gender and ethnicity, we have [test] only.")
    parser.add_argument("--skiptrain", action="store_true", help="Whether to skip the training and directly run eval on the dev set.")
    parser.add_argument("--replace2token", default=0, type=int, help="whether to replace the interventions or not. 0-keep the original interventions; 1-replace the intervetions to single 'ethical' or 'adversarial' token; 2-remove the interventions")
    parser.add_argument(
        "--evaluate_during_training", action="store_true", help="Run evaluation during training at each logging step."
    )
    parser.add_argument("--per_gpu_train_batch_size", default=32, type=int, help="Batch size per GPU/CPU for training.")
    parser.add_argument("--num_train", default=10000, type=int, help="num of examples used for fine tuning.")
    parser.add_argument(
        "--per_gpu_eval_batch_size", default=32, type=int, help="Batch size per GPU/CPU for evaluation."
    )
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument(
        "--gradient_accumulation_steps", type=int, default=1, help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight decay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float, help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument(
        "--num_train_epochs", default=3.0, type=float, help="Total number of training epochs to perform."
    )
    parser.add_argument(
        "--max_steps", default=-1, type=int, help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument("--warmup_steps", default=0, type=int, help="Linear warmup over warmup_steps.")

    parser.add_argument("--logging_steps", type=int, default=100, help="Log every X updates steps.")
    parser.add_argument("--save_steps", type=int, default=100, help="Save checkpoint every X updates steps.")
    parser.add_argument("--seed", type=int, default=42, help="random seed for initialization")
    parser.add_argument('--span_l', help="The maximal span length allowed for prediction", type=int, default=6)
    parser.add_argument("--eval_all_checkpoints", action="store_true",
        help="Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number",)
    parser.add_argument("--weight_ethical", type=float, default=1.0, help="default weight for ethical loss.")
    parser.add_argument("--weight_adversarial", type=float, default=1.0, help="default weight for adv loss.")
    parser.add_argument("--weight_irrelevant", type=float, default=1.0, help="default weight for irrelevant loss.")


    args = parser.parse_args()

    if (
        os.path.exists(args.output_dir)
        and os.listdir(args.output_dir)
        and args.train
        and not args.overwrite_output_dir
        and not args.skiptrain
    ):
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                args.output_dir
            )
        )


    # Setup CUDA, GPU & distributed training
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    args.n_gpu = torch.cuda.device_count()
    args.device = device

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO, 
    )
    logger.warning("Process device: %s, n_gpu: %s", device, args.n_gpu)

    # Set seed
    set_seed(args)

    # Load pretrained model and tokenizer
    print(">>> In model name: ", args.model_name_or_path)
    
    args.model_type = args.model_type.lower()

    # Modify the tokenizer. The Unqover tokenizer is causing issues due to having some old "add_special_tokens" in it, so manual modification is necessary.
    print("=== ABOUT TO OBTAIN THE TOKENIZER ===")

    download_dir = "./modified_tokenizer"

    os.makedirs(download_dir, exist_ok= True)

    tokenizer_files = [
        "tokenizer_config.json",
        "vocab.json",
        "merges.txt",
        "special_tokens_map.json",
        "config.json"
    ]

    for tokenizer_file in tokenizer_files:
        try:
            local_path = hf_hub_download(repo_id= args.model_name_or_path, filename = tokenizer_file)
            os.rename(local_path, os.path.join(download_dir, tokenizer_file))
        except Exception as e:
            print(f"!!! - Failed to download{tokenizer_file}: {e}")
    
    tokenizer_config_path = os.path.join(download_dir, "tokenizer_config.json")
    if os.path.exists(tokenizer_config_path):
        with open(tokenizer_config_path, "r", encoding= "utf-8") as f:
            config_json = json.load(f)
        
        if "add_special_tokens" in config_json:
            config_json.pop("add_special_tokens")
        
        with open(tokenizer_config_path, "w", encoding= "utf-8") as f:
            json.dump(config_json, f, indent= 2)

    tokenizer = RobertaTokenizerFast.from_pretrained(download_dir)
    config = AutoConfig.from_pretrained(download_dir)
    print("=== GOT THE TOKENIZER ===")
    
    #tokenizer = RobertaTokenizerFast.from_pretrained(args.model_name_or_path)
    model = mt.RobertaForQuestionAnsweringonUnqover.from_pretrained(args.model_name_or_path, config = config)
    '''
    tokenizer = mt.RobertaTokenizerFast.from_pretrained(args.model_name_or_path)
    model = mt.RobertaForQuestionAnsweringonUnqover.from_pretrained(args.model_name_or_path, config = config)
    '''
    model.to(args.device)

    logger.info("Training/evaluation parameters %s", args)

    


    # Training
    if args.train and not args.skiptrain:
        
        
        global_step, tr_loss = train(args, model, tokenizer)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)

        # Save the trained model and the tokenizer
        logger.info("Saving model checkpoint to %s", args.output_dir)
        # Save a trained model, configuration and tokenizer using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        # Take care of distributed/parallel training
        model_to_save = model.module if hasattr(model, "module") else model
        model_to_save.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(args, os.path.join(args.output_dir, "training_args.bin"))

        # Load a trained model and vocabulary that you have fine-tuned
        model = mt.RobertaForQuestionAnsweringonUnqover.from_pretrained(args.output_dir)  # , force_download=True)

        # SquadDataset is not compatible with Fast tokenizers which have a smarter overflow handeling
        # So we use use_fast=False here for now until Fast-tokenizer-compatible-examples are out
        tokenizer = mt.RobertaTokenizerFast.from_pretrained(args.output_dir)
        model.to(args.device)

    # Evaluation - we can ask to evaluate all the checkpoints (sub-directories) in a directory
    if args.eval:
        if args.train:
            logger.info("Loading checkpoints saved during training for evaluation")
            checkpoints = [args.output_dir]
            if args.eval_all_checkpoints:
                checkpoints = list(
                    os.path.dirname(c)
                    for c in sorted(glob.glob(args.output_dir + "/**/" + WEIGHTS_NAME, recursive=True))
                )
                logging.getLogger("transformers.modeling_utils").setLevel(logging.WARN)
            
        else:
            logger.info("Loading checkpoint %s for evaluation", args.model_name_or_path)
            checkpoints = [args.model_name_or_path]
            if args.eval_all_checkpoints:
                checkpoints = list(
                    os.path.dirname(c)
                    for c in sorted(glob.glob(args.model_name_or_path + "/**/" + WEIGHTS_NAME, recursive=True))
                )
                logging.getLogger("transformers.modeling_utils").setLevel(logging.WARN)

        logger.info("Evaluate the following checkpoints: %s", checkpoints)

        for checkpoint in checkpoints:
            # Reload the model
            global_step = checkpoint.split("-")[-1] if len(checkpoints) > 1 else ""
            model = mt.RobertaForQuestionAnsweringonUnqover.from_pretrained(checkpoint)  # , force_download=True)
            model.to(args.device)

            # Evaluate
            result = evaluate(args, model, tokenizer, prefix=global_step)
            logger.info("dev loss in noiid {} with ckp-{}: {}".format(args.evaldev, checkpoint, np.average(result)))


if __name__ == '__main__':
    main()
