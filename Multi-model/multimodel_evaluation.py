"""Console script for clip_benchmark."""

'''
Acknowledgement: This file is heavily derived from the CLIP_Benchmark framework, 
which provided the foundational architecture and methodology for this work."  
'''

import argparse
import csv
import json
import os
import random
import sys
from copy import copy
from itertools import product

import torch


from clip_benchmark.datasets.builder import (build_dataset, dataset_collection,
                                             get_dataset_collate_fn,
                                             get_dataset_collection_from_file,
                                             get_dataset_default_task)
from clip_benchmark.metrics import (captioning, image_caption_selection,
                                    linear_probe, zeroshot_classification,
                                    zeroshot_retrieval)
from clip_benchmark.model_collection import (get_model_collection_from_file,
                                             model_collection)
from clip_benchmark.models import MODEL_TYPES, load_clip
from mm_retrieval import mm_evaluate
from collections import OrderedDict
from model_zoo import CSR

def get_parser_args():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    parser_eval = subparsers.add_parser('eval', help='Evaluate') #flickr30k mscoco_captions
    parser_eval.add_argument('--dataset', type=str, default="flickr30k ", nargs="+", help="Dataset(s) to use for the benchmark. Can be the name of a dataset, or a collection name ('vtab', 'vtab+', 'imagenet_robustness', 'retrieval') or path of a text file where each line is a dataset name")
    parser_eval.add_argument('--dataset_root', default="root", type=str, help="dataset root folder where the datasets are downloaded. Can be in the form of a template depending on dataset name, e.g., --dataset_root='datasets/{dataset}'. This is useful if you evaluate on multiple datasets.")
    parser_eval.add_argument('--split', type=str, default="test", help="Dataset split to use")
    parser_eval.add_argument('--test_split', dest="split", action='store', type=str, default="test", help="Dataset split to use")
    parser_eval.add_argument('--train_split', type=str, nargs="+", default="train", help="Dataset(s) train split names")
    mutually_exclusive = parser_eval.add_mutually_exclusive_group()
    mutually_exclusive.add_argument('--val_split', default=None, type=str, nargs="+", help="Dataset(s) validation split names. Mutually exclusive with val_proportion.")
    mutually_exclusive.add_argument('--val_proportion', default=None, type=float, nargs="+", help="what is the share of the train dataset will be used for validation part, if it doesn't predefined. Mutually exclusive with val_split")
    parser_eval.add_argument('--model', type=str, nargs="+", default=["ViT-B-16"], help="Model architecture to use from OpenCLIP")
    parser_eval.add_argument('--pretrained', type=str, nargs="+", default=["dfn2b"], help="Model checkpoint name to use from OpenCLIP")
    parser_eval.add_argument('--pretrained_model', type=str, default="", nargs="+", help="Pre-trained model(s) to use. Can be the full model name where `model` and `pretrained` are comma separated (e.g., --pretrained_model='ViT-B-32-quickgelu,laion400m_e32'), a model collection name ('openai' or 'openclip_base' or 'openclip_multilingual' or 'openclip_all'), or path of a text file where each line is a model fullname where model and pretrained are comma separated (e.g., ViT-B-32-quickgelu,laion400m_e32). --model and --pretrained are ignored if --pretrained_model is used.")
    parser_eval.add_argument('--task', type=str, default="zeroshot_retrieval", choices=["zeroshot_classification", "zeroshot_retrieval", "linear_probe", "captioning", "image_caption_selection", "auto"], help="Task to evaluate on. With --task=auto, the task is automatically inferred from the dataset.")
    parser_eval.add_argument('--no_amp', action="store_false", dest="amp", default=True, help="whether to use mixed precision")
    parser_eval.add_argument('--num_workers', default=4, type=int)
    parser_eval.add_argument('--recall_k', default=[5,10], type=int, help="for retrieval, select the k for Recall@K metric. ", nargs="+",)
    parser_eval.add_argument('--fewshot_k', default=-1, type=int, help="for linear probe, how many shots. -1 = whole dataset.")
    parser_eval.add_argument('--fewshot_epochs', default=10, type=int, help="for linear probe, how many epochs.")
    parser_eval.add_argument('--fewshot_lr', default=0.1, type=float, help="for linear probe, what is the learning rate.")
    parser_eval.add_argument("--skip_load", action="store_true", help="for linear probes, when everything is cached, no need to load model.")
    parser_eval.add_argument("--distributed", action="store_true", help="evaluation in parallel")
    parser_eval.add_argument('--seed', default=0, type=int, help="random seed.")
    parser_eval.add_argument('--batch_size', default=64, type=int)
    parser_eval.add_argument('--normalize', default=True, type=bool, help="features normalization")
    parser_eval.add_argument('--model_cache_dir', default=None, type=str, help="directory to where downloaded models are cached")
    parser_eval.add_argument('--feature_root', default="features", type=str, help="feature root folder where the features are stored.")
    parser_eval.add_argument('--annotation_file', default="", type=str, help="text annotation file for retrieval datasets. Only needed  for when `--task` is `zeroshot_retrieval`.")
    parser_eval.add_argument('--custom_classname_file', default=None, type=str, help="use custom json file with classnames for each dataset, where keys are dataset names and values are list of classnames.")
    parser_eval.add_argument('--custom_template_file', default=None, type=str, help="use custom json file with prompts for each dataset, where keys are dataset names and values are list of prompts. For instance, to use CuPL prompts, use --custom_template_file='cupl_prompts.json'")
    parser_eval.add_argument('--dump_classnames', default=False, action="store_true", help="dump classnames to the results json file.")
    parser_eval.add_argument('--dump_templates', default=False, action="store_true", help="dump templates to the results json file.")

    parser_eval.add_argument('--language', default="en", type=str, nargs="+", help="language(s) of classname and prompts to use for zeroshot classification.")
    parser_eval.add_argument('--output', default="result.json", type=str, help="output file where to dump the metrics. Can be in form of a template, e.g., --output='{dataset}_{pretrained}_{model}_{language}_{task}.json'")
    parser_eval.add_argument('--quiet', dest='verbose', action="store_false", help="suppress verbose messages")
    parser_eval.add_argument('--save_clf', default=None, type=str, help="optionally save the classification layer output by the text tower")
    parser_eval.add_argument('--load_clfs', nargs='+', default=[], type=str, help="optionally load and average mutliple layers output by text towers.")
    parser_eval.add_argument('--skip_existing', default=False, action="store_true", help="whether to skip an evaluation if the output file exists.")
    parser_eval.add_argument('--model_type', default="open_clip", type=str, choices=MODEL_TYPES, help="clip model type")
    parser_eval.add_argument('--wds_cache_dir', default=None, type=str, help="optional cache directory for webdataset only")
    parser_eval.set_defaults(which='eval')

    ## Added for CSR embeds

    # CSR parameters
    parser_eval.add_argument('--csr', default=True, type=bool, help='whether to use CSR model')
    parser_eval.add_argument('--topk', default=None, type=int, dest='topk',
                        help='the number of topk')
    parser_eval.add_argument('--auxk', default=512, type=int, dest='auxk',
                        help='the number of auxk')
    parser_eval.add_argument('--auxk_coef', default=1 / 32, type=float, dest='auxk_coef',
                        help='auxk_coef')
    parser_eval.add_argument('--dead_threshold', default=5, type=int, dest='dead_threshold',
                        help='dead_threshold')
    parser_eval.add_argument('--input_dim', default=512, type=int, dest='input_dim',
                        help='input dim of ViT-B')
    parser_eval.add_argument('--hidden-size', default=512*4, type=int, dest='hidden_size',
                        help='the size of hidden layer')
    parser_eval.add_argument('--csr_ckpt', default=None, type=str, dest='csr_ckpt',
                        help='ckpt path of csr')


    # MRL parapmeters
    parser_eval.add_argument('--mrl', default=False,type=bool, help='whether to use mrl model')
    parser_eval.add_argument('--rep_dim', default=512, type=int, help='the number of auxk')

    parser_build = subparsers.add_parser('build', help='Build CSV from evaluations')
    parser_build.add_argument('files', type=str,  nargs="+", help="path(s) of JSON result files")
    parser_build.add_argument('--output', type=str,  default="benchmark.csv", help="CSV output file")
    parser_build.set_defaults(which='build')



    
    args = parser.parse_args()
    return parser, args

def main():
    parser, base = get_parser_args()
    if not hasattr(base, "which"):
        parser.print_help()
        return
    if base.which == "eval":
        main_eval(base)
    elif base.which == "build":
        main_build(base)

def main_build(base):
    # Build a benchmark single CSV file from a set of evaluations (JSON files)
    rows = []
    fieldnames = set()
    def process_file(path: str):
        data = json.load(open(path))
        row = {}
        row.update(data["metrics"])
        row.update(data)
        del row["metrics"]
        row['model_fullname'] = row['model'] + ' ' + row['pretrained']
        for field in row.keys():
            fieldnames.add(field)
        rows.append(row)
    for path in base.files:
        if os.path.isdir(path):
            files = [os.path.join(path, f) for f in os.listdir(path) if f.endswith(".json")]
            for file in files:
                process_file(file)
        else:
            process_file(path)
    with open(base.output, 'w') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

def main_eval(base):
    # Get list of pre-trained models to evaluate
    pretrained_model = _as_list(base.pretrained_model)
    if pretrained_model:
        models = []
        for name in pretrained_model:
            if os.path.isfile(name):
                # if path, read file, each line is a pre-trained model
                models.extend(get_model_collection_from_file(name))
            elif name in model_collection:
                # if part of `model_collection`, retrieve from it
                models.extend(model_collection[name])
            else:
                # if not, assume it is in the form of `model,pretrained`
                model, pretrained = name.split(',')
                models.append((model, pretrained))
    else:
        models = list(product(base.model, base.pretrained))

    # Ge list of datasets to evaluate on
    datasets = []
    for name in _as_list(base.dataset):
        if os.path.isfile(name):
            # If path, read file, each line is a dataset name
            datasets.extend(get_dataset_collection_from_file(name))
        elif name in dataset_collection:
            # if part of `dataset_collection`, retrieve from it
            datasets.extend(dataset_collection[name])
        else:
            # if not, assume it is simply the name of the dataset
            datasets.append(name)

    train_splits = _as_list(base.train_split)
    train_splits = _single_option_to_multiple_datasets(train_splits, datasets, "train_split")
    proportions, val_splits = None, None
    if base.val_split is not None:
        val_splits = _as_list(base.val_split)
        val_splits = _single_option_to_multiple_datasets(val_splits, datasets, "val_split")
    if base.val_proportion is not None:
        proportions = _as_list(base.val_proportion)
        proportions = _single_option_to_multiple_datasets(proportions, datasets, "val_proportion")

    dataset_info = {}
    for i in range(len(datasets)):
        dataset_info[datasets[i]] = {
            "train_split": train_splits[i],
            "val_split": val_splits[i] if val_splits is not None else None,
            "proportion": proportions[i] if proportions is not None else None
        }

    # Get list of languages to evaluate on
    languages = _as_list(base.language)

    if base.verbose:
        print(f"Models: {models}")
        print(f"Datasets: {datasets}")
        print(f"Languages: {languages}")
    runs = product(models, datasets, languages)
    if base.distributed:
        local_rank, rank, world_size = world_info_from_env()
        runs = list(runs)
        # randomize runs so that runs are balanced across gpus
        random.seed(base.seed)
        random.shuffle(runs)
        runs = [r for i, r in enumerate(runs) if i % world_size == rank]
    for (model, pretrained), (dataset), (language) in runs:
        # We iterative over all possible model/dataset/languages
        args = copy(base)
        args.model = model
        args.pretrained = pretrained
        args.dataset = dataset
        args.language = language
        args.train_split = dataset_info[dataset]["train_split"]
        args.val_split = dataset_info[dataset]["val_split"]
        args.val_proportion = dataset_info[dataset]["proportion"]
        run(args)

def _as_list(l):
    if not l:
        return []
    return [l] if type(l) != list else l

def _single_option_to_multiple_datasets(cur_option, datasets, name):
    cur_len = len(cur_option)
    ds_len = len(datasets)
    if cur_len != ds_len:
        # If user wants to use same value for all datasets
        if cur_len == 1:
            return [cur_option[0]] * ds_len
        else:
            raise ValueError(f"The incommensurable number of {name}")
    else:
        return cur_option

def run(args):
    """Console script for clip_benchmark."""
    if torch.cuda.is_available():
        if args.distributed:
            local_rank, rank, world_size = world_info_from_env()
            device = 'cuda:%d' % local_rank
            torch.cuda.set_device(device)
        else:
            device = "cuda"
        args.device = device
    else:
        args.device = "cpu"
    # set seed.
    torch.manual_seed(args.seed)
    task = args.task
    if args.dataset.startswith("wds/"):
        dataset_name = args.dataset.replace("wds/", "", 1)
    else:
        dataset_name = args.dataset
    if task == "auto":
        task = get_dataset_default_task(dataset_name)
    pretrained_slug = os.path.basename(args.pretrained) if os.path.isfile(args.pretrained) else args.pretrained
    pretrained_slug_full_path = args.pretrained.replace('/', '_') if os.path.isfile(args.pretrained) else args.pretrained
    dataset_slug = dataset_name.replace('/', '_')
    output = args.output.format(
        model=args.model,
        pretrained=pretrained_slug,
        pretrained_full_path=pretrained_slug_full_path,
        task=task,
        dataset=dataset_slug,
        language=args.language
    )
    if os.path.exists(output) and args.skip_existing:
        if args.verbose:
            print(f"Skip {output}, exists already.")
        return
    if args.verbose:
        print(f"Running '{task}' on '{dataset_name}' with the model '{args.pretrained}' on language '{args.language}'")
    dataset_root = args.dataset_root.format(dataset=dataset_name, dataset_cleaned=dataset_name.replace("/", "-"))
    if args.skip_load:
        model, transform, collate_fn, dataloader = None, None, None, None
    else:
        model, transform, tokenizer = load_clip(
            model_type=args.model_type,
            model_name=args.model,
            pretrained=args.pretrained,
            cache_dir=args.model_cache_dir,
            device=args.device
        )
        model.eval()
        if args.model.count("nllb-clip") > 0:
            # for NLLB-CLIP models, we need to set the language prior to running the tests
            from clip_benchmark.models.nllb_clip import set_language

            set_language(tokenizer, args.language)

        #Added for csr embeds
        if args.csr:
            csr_layer = CSR(n_latents=args.hidden_size, topk=args.topk,auxk=args.auxk,
                    normalize=False,n_inputs=args.input_dim,dead_threshold=args.dead_threshold)
            state = torch.load(args.csr_ckpt, map_location='cpu')['state_dict']
            state_clean = OrderedDict()

            for key in state.keys():
                if key.startswith('module.'):
                    state_clean[key.replace("module.", "")] = state[key]
                else:
                    state_clean[key] = state[key]

            csr_layer.load_state_dict(state_clean)
            csr_layer = csr_layer.cuda()
            csr_layer.eval()
            print('init csr layer')

        dataset = build_dataset(
            dataset_name=args.dataset,
            root=dataset_root,
            transform=transform,
            split=args.split,
            annotation_file=args.annotation_file,
            download=True,
            language=args.language,
            task=task,
            custom_template_file=args.custom_template_file,
            custom_classname_file=args.custom_classname_file,
            wds_cache_dir=args.wds_cache_dir,
        )
        collate_fn = get_dataset_collate_fn(args.dataset)
        if args.verbose:
            try:
                print(f"Dataset size: {len(dataset)}")
            except TypeError:
                print("IterableDataset has no len()")
            print(f"Dataset split: {args.split}")
            if hasattr(dataset, "classes") and dataset.classes:
                try:
                    print(f"Dataset classes: {dataset.classes}")
                    print(f"Dataset number of classes: {len(dataset.classes)}")
                except AttributeError:
                    print("Dataset has no classes.")

        if args.dataset.startswith("wds/"):
            dataloader = torch.utils.data.DataLoader(
                dataset.batched(args.batch_size), batch_size=None,
                shuffle=False, num_workers=args.num_workers,
            )
        else:
            dataloader = torch.utils.data.DataLoader(
                dataset, batch_size=args.batch_size,
                shuffle=False, num_workers=args.num_workers,
                collate_fn=collate_fn
            )
    if task == "zeroshot_retrieval":
        metrics = mm_evaluate(
            model,
            csr_layer,
            dataloader,
            tokenizer,
            recall_k_list=args.recall_k,
            device=args.device,
            amp=args.amp,
            csr=args.csr,
            rep_dim=args.rep_dim,
        )
    else:
        raise ValueError("Unsupported task: {}. task should be `zeroshot_classification`, `zeroshot_retrieval`, `linear_probe`, or `captioning`".format(task))
    dump = {
        "dataset": args.dataset,
        "model": args.model,
        "pretrained": args.pretrained,
        "task": task,
        "metrics": metrics,
        "language": args.language,
    }
    if hasattr(dataset, "classes") and dataset.classes and args.dump_classnames:
        dump["classnames"] = dataset.classes
    if hasattr(dataset, "templates") and dataset.templates and args.dump_templates:
        dump["templates"] = dataset.templates
    if args.verbose:
        print(f"Dump results to: {output}")
    with open(output, "w") as f:
        json.dump(dump, f)
    return 0


def world_info_from_env():
    # from openclip
    local_rank = 0
    for v in ('LOCAL_RANK', 'MPI_LOCALRANKID', 'SLURM_LOCALID', 'OMPI_COMM_WORLD_LOCAL_RANK'):
        if v in os.environ:
            local_rank = int(os.environ[v])
            break
    global_rank = 0
    for v in ('RANK', 'PMI_RANK', 'SLURM_PROCID', 'OMPI_COMM_WORLD_RANK'):
        if v in os.environ:
            global_rank = int(os.environ[v])
            break
    world_size = 1
    for v in ('WORLD_SIZE', 'PMI_SIZE', 'SLURM_NTASKS', 'OMPI_COMM_WORLD_SIZE'):
        if v in os.environ:
            world_size = int(os.environ[v])
            break
    return local_rank, global_rank, world_size


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
