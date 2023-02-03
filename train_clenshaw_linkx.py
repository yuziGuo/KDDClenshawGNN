import matplotlib.pyplot as plt
from data.linkx_dataloader import linkx_dataloader


from models.ChebClenshawNN import ChebNN
from utils.rocauc_eval import eval_rocauc

from utils.grading_logger import get_logger
from utils.stopper import EarlyStopping
import argparse
import random
import time 

import numpy as np
import torch as th
import torch.nn.functional as F
from torch_geometric.nn.conv.gcn_conv import gcn_norm


def build_dataset(args):
    if args.dataset in ['twitch-gamer', 'Penn94', 'genius']:
        loader = linkx_dataloader(args.dataset, args.gpu, args.self_loop, n_cv=args.n_cv, cv_id=args.start_cv)
    else:
        raise ValueError('Unknown dataset: {}'.format(args.dataset))
    loader.load_data()
    return loader

def build_model(args, edge_index, norm_A, in_feats, n_classes):
    if args.model == 'ChebNN':
        model = ChebNN(
                    edge_index,
                    norm_A,
                    in_feats,
                    args.n_hidden,
                    n_classes,
                    args.n_layers,
                    args.dropout,
                    args.dropout2,
                    args.lamda
                    )
        model.to(args.gpu)
        return model


def build_optimizers(args, model):
    param_groups = [
        {'params':model.params1, 'lr':args.lr1,'weight_decay':args.wd1}, # convs
        {'params':model.params2, 'lr':args.lr2,'weight_decay':args.wd2}, # fcs
        ]
    optimizer = th.optim.Adam(param_groups)

    param_groups = [
        {'params':[model.alpha_params], 'lr':args.lr3,'weight_decay':args.wd3}, # p
    ]
    optimizer_2 = th.optim.SGD(param_groups, momentum=args.momentum)
    return [optimizer, optimizer_2]


def build_stopper(args):
    stopper = EarlyStopping(patience=args.patience, store_path=args.es_ckpt+'.pt')
    step = stopper.step
    return step, stopper


def evaluate(model, loss_fcn, features, labels, mask, evaluator=None):
    model.eval()
    with th.no_grad():
        logits = model(features)
        if not th.is_tensor(logits):
            logits = logits[0]
        logits = logits[mask]
        labels = labels[mask]
        loss = loss_fcn(logits, labels)

        if evaluator == 'rocauc':
            rocauc = eval_rocauc(**{"y_pred": logits,
                                  "y_true": labels})
            return rocauc, loss
        else:
            _, indices = th.max(logits, dim=1)
            correct = th.sum(indices == labels)
            acc = correct.item() * 1.0 / len(labels)
        return acc, loss
    

def run(args, cv_id, edge_index, data, norm_A, features, labels, model_seed):
    dur = []
    
    if args.dataset in ['twitch-gamer', 'Penn94', 'genius']: # encouraged to use fixed splits
        data.load_mask()
    else:
        data.load_mask(p=(0.6,0.2,0.2))
    
    logger.info('#Train:{}'.format(data.train_mask.sum().item()))
    reset_random_seeds(model_seed)
    if args.dataset != 'genius':    
        loss_fcn = th.nn.NLLLoss()
        evaluator = 'acc'
    else:
        loss_fcn = th.nn.BCEWithLogitsLoss()
        evaluator = 'rocauc'
        labels = F.one_hot(labels, labels.max()+1).float()


    data.in_feats = features.shape[-1] 
    model = build_model(args, edge_index, norm_A, data.in_feats, data.n_classes)
    optimizers = build_optimizers(args, model)
    if args.early_stop:
        stopper_step, stopper = build_stopper(args)
    

    for epoch in range(args.n_epochs): 
        t0 = time.time()
        
        model.train()
        for _ in optimizers:
            _.zero_grad()
        
        logits = model(features)
        loss = loss_fcn(logits[data.train_mask], labels[data.train_mask])
        loss.backward()
        # print(loss.item())

        for _ in optimizers:
            _.step()

        val_acc, val_loss = evaluate(model, loss_fcn, features, labels, data.val_mask, evaluator=evaluator)

        dur.append(time.time() - t0)
        if args.log_detail and (epoch+1) % 10 == 0 :
            logger.info("Epoch {:05d} | Time(s) {:.4f} | Val Loss {:.4f} | Val Acc {:.4f}| "
                        "ETputs(KTEPS) {:.2f}". format(epoch+1, np.mean(dur), val_loss.item(),
                                                        val_acc,  
                                                        data.n_edges / np.mean(dur) / 100)
                        )
        if args.early_stop and epoch >= 0:
            score =  val_loss
            if stopper_step(score, model):
                break   
    # end for

    if args.early_stop:
        model.load_state_dict(th.load(stopper.store_path))
        logger.debug('Model Saved by Early Stopper is Loaded!')
    val_acc, val_loss = evaluate(model, loss_fcn, features, labels, data.val_mask, evaluator=evaluator)
    logger.info("[FINAL MODEL] Run {} .\Val accuracy {:.2%} \Val loss: {:.2}".format(cv_id+args.start_cv, val_acc, val_loss))
    test_acc, test_loss = evaluate(model, loss_fcn, features, labels, data.test_mask, evaluator=evaluator)
    logger.info("[FINAL MODEL] Run {} .\tTest accuracy {:.2%} \Test loss: {:.2}".format(cv_id+args.start_cv, test_acc, test_loss))
    return val_acc, test_acc

    

def main(args):
    reset_random_seeds(args.seed)
    data  = build_dataset(args)
    data.seeds = [random.randint(0,10000) for i in range(args.n_cv)]
    model_seeds = [random.randint(0,10000) for _ in range(args.n_cv)]
    logger.info('Split_seeds:{:s}'.format(str(data.seeds)))
    logger.info('Model_seeds:{:s}'.format(str(model_seeds)))

    edge_index = data.edge_index
    # Authors note for L165:
    # Always set add_self_loops=False here.
    # If args.self_loop is True, the selfloops will be added in dataloader 
    _, norm_A = gcn_norm(edge_index, add_self_loops=False)
    features = data.features
    labels = data.labels

    accs = []
    val_accs = []
    
    for cv_id in range(args.n_cv):
        val_acc, test_acc = run(args, cv_id, edge_index, data, norm_A,  features, labels, model_seed=model_seeds[cv_id])
        accs.append(test_acc)
        val_accs.append(val_acc)

    logger.info("Mean Acc For Cross Validation: {:.4f}, STDV: {:.4f}".format(np.array(accs).mean(), np.array(accs).std()))
    logger.info(accs)
    
def set_args():
    parser = argparse.ArgumentParser(description='GCN')
    parser.add_argument('--seed', type=int, default=42, help='Random seed.')
    parser.add_argument("--model", type=str, default='ChebNN')
    parser.add_argument("--gpu", type=int, default=0, help="gpu")
    parser.add_argument("--dataset", type=str, default="cora", help="Dataset name ('cora', 'citeseer', 'pubmed').")
    parser.add_argument("--ds-split", type=str, default="standard", help="split by ('standard', 'mg', 'random').")
    parser.add_argument('--split-seed', type=str, default='random', help='(predefined, random)')

    # for model configuration 
    parser.add_argument("--n-layers", type=int, default=2, help="number of hidden gcn layers")
    parser.add_argument("--n-hidden", type=int, default=64, help="number of hidden gcn units")

    # for training
    parser.add_argument("--wd1", type=float, default=1e-2, help="Weight for L2 loss")
    parser.add_argument("--wd2", type=float, default=5e-4, help="Weight for L2 loss")
    parser.add_argument("--wd3", type=float, default=5e-4, help="Weight for L2 loss")
    parser.add_argument("--lr1",  type=float, default=1e-2, help="learning rate")
    parser.add_argument("--lr2",  type=float, default=1e-2, help="learning rate")
    parser.add_argument("--lr3",  type=float, default=1e-2, help="learning rate")
    parser.add_argument("--momentum",  type=float, default=0.9, help="SGD momentum")
    parser.add_argument("--lamda", type=float, default=1.0, help="lamda in  GCNII")
    parser.add_argument("--n-epochs", type=int, default=2000, help="number of training epochs")
    parser.add_argument("--dropout", type=float, default=0.5, help="dropout probability")
    parser.add_argument("--dropout2", type=float, default=0.7, help="dropout probability")

    parser.add_argument("--loss", type=str, default='nll')
    parser.add_argument("--self-loop", action='store_true', default=False, help="graph self-loop (default=False)")
    parser.add_argument("--udgraph", action='store_true', default=False, help="undirected graph (default=False)")

    # for experiment running
    parser.add_argument("--early-stop", action='store_true', default=False, help="early stop (default=False)")
    parser.add_argument("--patience", type=int, default=300, help="patience for early stop")
    parser.add_argument("--es-ckpt", type=str, default="es_checkpoint", help="Saving directory for early stop checkpoint")
    parser.add_argument("--n-cv", type=int, default=5, help="Number of cross validations")
    parser.add_argument("--start-cv", type=int, default=0, help="Start index of cross validations (option for debug)")
    
    # log options
    parser.add_argument("--logging", action='store_true', default=False, help="log results and details to files (default=False)")
    parser.add_argument("--log-detail", action='store_true', default=False)
    parser.add_argument("--log-detailedCh", action='store_true', default=False)
    parser.add_argument("--id-log", type=int, default=0)

    args = parser.parse_args()

    if args.gpu < 0:
        args.gpu = 'cpu'

    if args.es_ckpt == 'es_checkpoint':
        args.es_ckpt = '_'.join([args.es_ckpt, 'device='+str(args.gpu)])

    return args


def reset_random_seeds(seed):
    random.seed(seed)
    np.random.seed(seed)
    th.manual_seed(seed)
    th.cuda.manual_seed(seed) 

def set_logger(args):
    if args.id_log > 0:
        log_d = 'runs/Logs'+str(args.id_log)
        logger = get_logger(file_mode=args.logging, dir_name=log_d)
    else:
        logger = get_logger(file_mode=args.logging, detailedConsoleHandler=args.log_detailedCh)
    return logger


if __name__=='__main__':
    args = set_args()
    logger = set_logger(args)
    logger.info(args)
    main(args)