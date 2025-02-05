import asyncio
import json
import os
from functools import partial

import orjson
import torch
import time
from simple_parsing import ArgumentParser

from delphi.clients import Offline
from delphi.config import ExperimentConfig, FeatureConfig
from delphi.explainers import DefaultExplainer
from delphi.features import (
    FeatureDataset,
    FeatureLoader
)
from delphi.features.constructors import default_constructor
from delphi.features.samplers import sample
from delphi.pipeline import Pipe,Pipeline, process_wrapper
from delphi.scorers import FuzzingScorer, DetectionScorer


def main(args):
    module = args.module
    feature_cfg = args.feature_options
    experiment_cfg = args.experiment_options
    shown_examples = args.shown_examples
    n_features = args.features  
    start_feature = args.start_feature
    sae_model = args.model
    activations = args.activations
    cot = args.cot
    quantization = args.quantization
    feature_dict = {f"{module}": torch.arange(start_feature,start_feature+n_features)}
    dataset = FeatureDataset(
        raw_dir=f"raw_features/{sae_model}",
        cfg=feature_cfg,
        modules=[module],
        features=feature_dict,
    )

    
    constructor=partial(
            default_constructor,
            tokens=dataset.tokens,
            n_random=experiment_cfg.n_random, 
            ctx_len=experiment_cfg.example_ctx_len, 
            max_examples=feature_cfg.max_examples
        )
    sampler=partial(sample,cfg=experiment_cfg)
    loader = FeatureLoader(dataset, constructor=constructor, sampler=sampler)
    ### Load client ###
    if args.quantization == "awq":
        client = Offline("hugging-quants/Meta-Llama-3.1-70B-Instruct-AWQ-INT4",max_memory=0.8,max_model_len=5120)
    elif args.quantization == "none":
        client = Offline("meta-llama/Meta-Llama-3.1-70B-Instruct",max_memory=0.8,max_model_len=5120,num_gpus=2)

    ### Build Explainer pipe ###
    def explainer_postprocess(result):

        with open(f"results/explanations/{sae_model}/{experiment_name}/{result.record.feature}.txt", "wb") as f:
            f.write(orjson.dumps(result.explanation))

        return result
    #try making the directory if it doesn't exist
    os.makedirs(f"results/explanations/{sae_model}/{experiment_name}", exist_ok=True)

    explainer_pipe = process_wrapper(
        DefaultExplainer(
            client, 
            tokenizer=dataset.tokenizer,
            threshold=0.3,
            activations=activations,
            cot=cot
        ),
        postprocess=explainer_postprocess,
    )

    #save the experiment config
    with open(f"results/explanations/{sae_model}/{experiment_name}/experiment_config.json", "w") as f:
        print(experiment_cfg.to_dict())
        f.write(json.dumps(experiment_cfg.to_dict()))

    ### Build Scorer pipe ###

    def scorer_preprocess(result):
        record = result.record
        record.explanation = result.explanation
        record.extra_examples = record.random_examples

        return record

    def scorer_postprocess(result, score_dir):
        record = result.record
        with open(f"results/scores/{sae_model}/{experiment_name}/{score_dir}/{record.feature}.txt", "wb") as f:
            f.write(orjson.dumps(result.score))
        

    os.makedirs(f"results/scores/{sae_model}/{experiment_name}/detection", exist_ok=True)
    os.makedirs(f"results/scores/{sae_model}/{experiment_name}/fuzz", exist_ok=True)

    #save the experiment config
    with open(f"results/scores/{sae_model}/{experiment_name}/detection/experiment_config.json", "w") as f:
        f.write(json.dumps(experiment_cfg.to_dict()))

    with open(f"results/scores/{sae_model}/{experiment_name}/fuzz/experiment_config.json", "w") as f:
        f.write(json.dumps(experiment_cfg.to_dict()))


    scorer_pipe = Pipe(process_wrapper(
            DetectionScorer(client, tokenizer=dataset.tokenizer, batch_size=shown_examples,verbose=False,log_prob=True),
            preprocess=scorer_preprocess,
            postprocess=partial(scorer_postprocess, score_dir="detection"),
        ),
        process_wrapper(
            FuzzingScorer(client, tokenizer=dataset.tokenizer, batch_size=shown_examples,verbose=False,log_prob=True),
            preprocess=scorer_preprocess,
            postprocess=partial(scorer_postprocess, score_dir="fuzz"),
        )
    )

    ### Build the pipeline ###

    pipeline = Pipeline(
        loader,
        explainer_pipe,
        scorer_pipe,
    )
    start_time = time.time()
    asyncio.run(pipeline.run(50))
    end_time = time.time()
    print(f"Time taken: {end_time - start_time} seconds")
    

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--shown_examples", type=int, default=5)
    parser.add_argument("--model", type=str, default="128k")
    parser.add_argument("--module", type=str, default=".transformer.h.0")
    parser.add_argument("--features", type=int, default=100)
    parser.add_argument("--start_feature", type=int, default=0)
    parser.add_argument("--experiment_name", type=str, default="default")
    parser.add_argument("--no-activations", action="store_false", dest="activations", help="Disable activations")
    parser.add_argument("--cot", type=bool, default=False)
    parser.add_argument("--quantization", type=str, default="awq")
    parser.add_arguments(ExperimentConfig, dest="experiment_options")
    parser.add_arguments(FeatureConfig, dest="feature_options")
    args = parser.parse_args()
    print(args)
    experiment_name = args.experiment_name
    


    main(args)
