#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Generating T-trees from dialogue acts.

Usage: ./tgen.py <action> <argument1 ...>

Actions:

candgen_train -- train candidate generator (probability distributions)
    - arguments: [-p prune_threshold] train-das train-ttrees output-model

percrank_train -- train perceptron global ranker
    - arguments: [-d debug-output] [-c candgen-model] [-s data-portion] [-j parallel-jobs] [-w parallel-work-dir] \\
                 ranker-config train-das train-ttrees output-model

sample_gen -- generate using the given candidate generator
    - arguments: [-n trees-per-da] [-o oracle-eval-ttrees] [-w output-ttrees] candgen-model test-das

asearch_gen -- generate using the A*search sentence planner
    - arguments: [-e eval-ttrees-file] [-s eval-ttrees-selector] [-d debug-output] [-w output-ttrees] [-c config] candgen-model percrank-model test-das
"""

from __future__ import unicode_literals
import random
import sys
from getopt import getopt
import platform
import os

from alex.components.nlg.tectotpl.core.util import file_stream
from alex.components.nlg.tectotpl.core.document import Document
from alex.components.nlg.tectotpl.block.write.yaml import YAML as YAMLWriter

from flect.config import Config

from tgen.logf import log_info, set_debug_stream, log_debug
from tgen.futil import read_das, read_ttrees, chunk_list, add_bundle_text, \
    trees_from_doc, ttrees_from_doc
from tgen.candgen import RandomCandidateGenerator
from tgen.rank import PerceptronRanker
from tgen.planner import SamplingPlanner, ASearchPlanner
from tgen.eval import p_r_f1_from_counts, corr_pred_gold, f1_from_counts, ASearchListsAnalyzer, \
    EvalTypes, Evaluator
from tgen.parallel_percrank_train import ParallelPerceptronRanker


def candgen_train(args):
    opts, files = getopt(args, 'p:')
    prune_threshold = 1
    for opt, arg in opts:
        if opt == '-p':
            prune_threshold = int(arg)

    if len(files) != 3:
        sys.exit(__doc__)
    fname_da_train, fname_ttrees_train, fname_cand_model = files

    log_info('Training candidate generator...')
    candgen = RandomCandidateGenerator({'prune_threshold': prune_threshold})
    candgen.train(fname_da_train, fname_ttrees_train)
    candgen.save_model(fname_cand_model)


def percrank_train(args):
    opts, files = getopt(args, 'c:d:s:j:w:')
    candgen_model = None
    train_size = 1.0
    parallel = False
    jobs_number = 0
    work_dir = None

    for opt, arg in opts:
        if opt == '-d':
            set_debug_stream(file_stream(arg, mode='w'))
        elif opt == '-s':
            train_size = float(arg)
        elif opt == '-c':
            candgen_model = arg
        elif opt == '-j':
            parallel = True
            jobs_number = int(arg)
        elif opt == '-w':
            work_dir = arg

    if len(files) != 4:
        sys.exit(__doc__)

    fname_rank_config, fname_train_das, fname_train_ttrees, fname_rank_model = files
    log_info('Training perceptron ranker...')

    rank_config = Config(fname_rank_config)
    if candgen_model:
        rank_config['candgen_model'] = candgen_model
    if not parallel:
        ranker = PerceptronRanker(rank_config)
    else:
        rank_config['jobs_number'] = jobs_number
        if work_dir is None:
            work_dir, _ = os.path.split(fname_rank_config)
        ranker = ParallelPerceptronRanker(rank_config, work_dir)
    ranker.train(fname_train_das, fname_train_ttrees, data_portion=train_size)
    ranker.save_to_file(fname_rank_model)


def sample_gen(args):
    opts, files = getopt(args, 'r:n:o:w:')
    num_to_generate = 1
    oracle_eval_file = None
    fname_ttrees_out = None

    for opt, arg in opts:
        if opt == '-n':
            num_to_generate = int(arg)
        elif opt == '-o':
            oracle_eval_file = arg
        elif opt == '-w':
            fname_ttrees_out = arg

    if len(files) != 2:
        sys.exit(__doc__)
    fname_cand_model, fname_da_test = files

    # load model
    log_info('Initializing...')
    candgen = RandomCandidateGenerator({})
    candgen.load_model(fname_cand_model)

    ranker = candgen

    tgen = SamplingPlanner({'candgen': candgen, 'ranker': ranker})
    # generate
    log_info('Generating...')
    gen_doc = Document()
    das = read_das(fname_da_test)
    for da in das:
        for _ in xrange(num_to_generate):  # repeat generation n times
            tgen.generate_tree(da, gen_doc)

    # evaluate if needed
    if oracle_eval_file is not None:
        log_info('Evaluating oracle F1...')
        log_info('Loading gold data from ' + oracle_eval_file)
        gold_trees = ttrees_from_doc(read_ttrees(oracle_eval_file), tgen.language, tgen.selector)
        gen_trees = ttrees_from_doc(gen_doc, tgen.language, tgen.selector)
        log_info('Gold data loaded.')
        correct, predicted, gold = 0, 0, 0
        for gold_tree, gen_trees in zip(gold_trees, chunk_list(gen_trees, num_to_generate)):
            # find best of predicted trees (in terms of F1)
            _, tc, tp, tg = max([(f1_from_counts(c, p, g), c, p, g) for c, p, g
                                 in map(lambda gen_tree: corr_pred_gold(gold_tree, gen_tree),
                                        gen_trees)],
                                key=lambda x: x[0])
            correct += tc
            predicted += tp
            gold += tg
        # evaluate oracle F1
        log_info("Oracle Precision: %.6f, Recall: %.6f, F1: %.6f" % p_r_f1_from_counts(correct, predicted, gold))
    # write output
    if fname_ttrees_out is not None:
        log_info('Writing output...')
        writer = YAMLWriter(scenario=None, args={'to': fname_ttrees_out})
        writer.process_document(gen_doc)


def asearch_gen(args):
    """A*search generation"""

    opts, files = getopt(args, 'e:d:w:c:')
    eval_file = None
    fname_ttrees_out = None
    cfg_file = None
    eval_selector = ''

    for opt, arg in opts:
        if opt == '-e':
            eval_file = arg
        elif opt == '-s':
            eval_selector = arg
        elif opt == '-d':
            set_debug_stream(file_stream(arg, mode='w'))
        elif opt == '-w':
            fname_ttrees_out = arg
        elif opt == '-c':
            cfg_file = arg

    if len(files) != 3:
        sys.exit('Invalid arguments.\n' + __doc__)
    fname_cand_model, fname_rank_model, fname_da_test = files

    log_info('Initializing...')
    candgen = RandomCandidateGenerator({})
    candgen.load_model(fname_cand_model)
    ranker = PerceptronRanker.load_from_file(fname_rank_model)
    cfg = Config(cfg_file) if cfg_file else {}
    cfg.update({'candgen': candgen, 'ranker': ranker})
    tgen = ASearchPlanner(cfg)

    log_info('Generating...')
    das = read_das(fname_da_test)

    if eval_file is None:
        gen_doc = Document()
    else:
        eval_doc = read_ttrees(eval_file)
        if eval_selector == tgen.selector:
            gen_doc = Document()
        else:
            gen_doc = eval_doc

    # generate and evaluate
    if eval_file is not None:
        # generate + analyze open&close lists
        lists_analyzer = ASearchListsAnalyzer()
        for num, (da, gold_tree) in enumerate(zip(das,
                                                  trees_from_doc(eval_doc, tgen.language, eval_selector))):
            log_debug("\n\nTREE No. %03d" % num)
            open_list, close_list = tgen.generate_tree(da, gen_doc, return_lists=True)
            lists_analyzer.append(gold_tree, open_list, close_list)
        log_info('Gold tree BEST: %.4f, on CLOSE: %.4f, on ANY list: %4f' % lists_analyzer.stats())

        # evaluate F-scores
        eval_ttrees = ttrees_from_doc(eval_doc, tgen.language, eval_selector)
        gen_ttrees = ttrees_from_doc(gen_doc, tgen.language, tgen.selector)

        log_info('Evaluating...')
        evaler = Evaluator()
        for eval_bundle, eval_ttree, gen_ttree in zip(eval_doc.bundles, eval_ttrees, gen_ttrees):
            add_bundle_text(eval_bundle, tgen.language, tgen.selector + 'Xscore',
                            "P: %.4f R: %.4f F1: %.4f" % p_r_f1_from_counts(*corr_pred_gold(eval_ttree, gen_ttree)))
            evaler.append(eval_ttree, gen_ttree)
        log_info("NODE precision: %.4f, Recall: %.4f, F1: %.4f" % evaler.p_r_f1())
        log_info("DEP  precision: %.4f, Recall: %.4f, F1: %.4f" % evaler.p_r_f1(EvalTypes.DEP))
    # just generate
    else:
        for da in das:
            tgen.generate_tree(da, gen_doc)

    # write output
    if fname_ttrees_out is not None:
        log_info('Writing output...')
        writer = YAMLWriter(scenario=None, args={'to': fname_ttrees_out})
        writer.process_document(gen_doc)


if __name__ == '__main__':

    random.seed(1206)

    if len(sys.argv) < 3:
        sys.exit(__doc__)

    action = sys.argv[1]
    args = sys.argv[2:]

    log_info('Running on %s version %s' % (platform.python_implementation(),
                                           platform.python_version()))

    if action == 'candgen_train':
        candgen_train(args)
    elif action == 'percrank_train':
        percrank_train(args)
    elif action == 'random_gen':
        sample_gen(args)
    elif action == 'asearch_gen':
        asearch_gen(args)
    else:
        # Unknown action
        sys.exit(("\nERROR: Unknown Tgen action: %s\n\n---" % action) + __doc__)

    log_info('Done.')
