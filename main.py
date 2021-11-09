import argparse
import os
import random
import numpy as np

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torch.nn.functional as F
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
from torch.autograd import Variable
from torchvision import models
from torch.utils.data.sampler import SubsetRandomSampler, RandomSampler
torch.cuda.empty_cache()

from colorama import *

import scipy.spatial
from tqdm import tqdm
from collections import defaultdict
from scipy import misc
import time
import math

from utils import *
import attack_model
from models import *
#from pretrained_models_pytorch import pretrainedmodels

parser = argparse.ArgumentParser()
parser.add_argument('--workers', type=int, help='number of data loading workers', default=2)
parser.add_argument('--batchSize', type=int, default=128, help='input batch size')
parser.add_argument('--epochs', type=int, default=20, help='number of epochs to train for')
parser.add_argument('--lr', type=float, default=0.0002, help='learning rate, default=0.0002')
parser.add_argument('--beta1', type=float, default=0.5, help='beta1 for adam. default=0.5')

parser.add_argument('--optimize_on_success', type=int, default=0, help="whether to optimize on samples that are already successful \
                     - if set to 0, we only optimize on failed attempts to compute adv examples, removing the successes \
                     - if set to 1, we optimize on all samples")
parser.add_argument('--targeted', type=int, default=0, help='if the attack is targeted (default False)')
parser.add_argument('--chosen_target_class', type=int, default=0, help='int representing class to target')
parser.add_argument('--restrict_to_correct_preds', type=int, default=1, help='if 1, only compute adv examples on correct predictions')
parser.add_argument('--shrink', type=float, default=0.01, help='scale perturbation by this value')
parser.add_argument('--shrink_inc', type=float, default=0.01, help='update the scale value by this much when loss does not decrease')
parser.add_argument('--ldist_weight', type=float, default=4.0, help='how much to weight the ldist loss term')
parser.add_argument('--l2reg', type=float, default=0.01, help='weight factor for l2 regularization')
parser.add_argument('--max_norm', type=float, default=0.04, help='max allowed perturbation')
parser.add_argument('--norm', type=str, default='linf', help='l2 or linf')
parser.add_argument('--cuda', action='store_true', help='enables cuda')
parser.add_argument('--ngpu', type=int, default=1, help='number of GPUs to use')
parser.add_argument('--every', type=int, default=1, help='save if epoch is divisible by this')
parser.add_argument('--nz', type=int, default=100, help='size of the latent z vector')
parser.add_argument('--imageSize', type=int, default=299, help='the height / width of the input image to network')
parser.add_argument('--netAttacker', default='', help="path to netAttacker (to continue training)")
parser.add_argument('--netClassifier', default='./checkpoint/ckpt.pth', help="For CIFAR-10: path to netClassifier (to get target model predictions) \
                                                                             For ImageNet: type of classifier (e.g. inceptionV3)")
parser.add_argument('--outf', default='./logs', help='folder to output images and model checkpoints')
parser.add_argument('--manualSeed', type=int, default=5198, help='manual seed')
parser.add_argument('--dataset', type=str, default='ImageNet', help='dataset images path')

opt = parser.parse_args()
print(opt)

try:
    os.makedirs(opt.outf)
    os.makedirs('classifications')
except OSError:
    pass

WriteToFile('./%s/log' %(opt.outf), opt)
WriteToFile('./%s/classifications' %(opt.outf), opt)


class ToSpaceBGR(object):

    def __init__(self, is_bgr):
        self.is_bgr = is_bgr

    def __call__(self, tensor):
        if self.is_bgr:
            new_tensor = tensor.clone()
            new_tensor[0] = tensor[2]
            new_tensor[2] = tensor[0]
            tensor = new_tensor
        return tensor

class ToRange255(object):

    def __init__(self, is_255):
        self.is_255 = is_255

    def __call__(self, tensor):
        if self.is_255:
            tensor.mul_(255)
        return tensor

if opt.manualSeed is None:
    opt.manualSeed = random.randint(1, 10000)
print("Random Seed: ", opt.manualSeed)
random.seed(opt.manualSeed)
np.random.seed(opt.manualSeed)
torch.manual_seed(opt.manualSeed)
if opt.cuda:
    torch.cuda.manual_seed_all(opt.manualSeed)

cudnn.benchmark = True

if torch.cuda.is_available() and not opt.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

nc = 3
ngpu = int(opt.ngpu)


# set-up models and load weights if any are saved
netAttacker = attack_model._netAttacker(ngpu, opt.imageSize)
netAttacker.apply(weights_init)
if opt.netAttacker != '':
    netAttacker.load_state_dict(torch.load(opt.netAttacker))
    print("Net attacker loaded!!\n")

print("=> creating model ")
checkpoint = torch.load(opt.netClassifier)
net = DenseNet121()
net = net.to('cuda')
net = torch.nn.DataParallel(net)
cudnn.benchmark = True
net.load_state_dict(checkpoint['net'])
torch.cuda.empty_cache()
print('debug: emptied cache after net!')
netClassifier = net


if opt.cuda:
    netAttacker.cuda()
    netClassifier.cuda()


print('==> Preparing data..')
transform_train = transforms.Compose([
        transforms.Scale((opt.imageSize,opt.imageSize)),
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
    ])

transform_test = transforms.Compose([
    transforms.Scale((opt.imageSize,opt.imageSize)),
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

#loading of image data. Image data consits of batch_idx, input (image), targets (correct classification - groundtruth)
trainset = dset.ImageFolder(root = './data/train', transform = transform_train)
trainloader = torch.utils.data.DataLoader(
    trainset, batch_size=opt.batchSize, shuffle=True, num_workers=2)


testset = dset.ImageFolder(root = './data/test', transform = transform_test)
testloader = torch.utils.data.DataLoader(
    testset, batch_size=opt.batchSize, shuffle=False, num_workers=2)

classes = ('black', 'green','red', 'yellow')

 
# setup optimizer
optimizerAttacker = optim.Adam(netAttacker.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999), weight_decay=opt.l2reg)

# pre-set noise variable
noise = torch.FloatTensor(opt.batchSize, opt.nz, 1, 1)
if opt.cuda:
    noise = noise.cuda()
noise = Variable(noise)

def train(epoch, c, noise):
    # set-up structures to track norms, losses etc.
    netAttacker.train()
    netClassifier.eval()
    c_loss, L_inf, L2, pert_norm, dist, adv_norm, non_adv_norm = [ ], [ ], [ ], [ ], [ ], [ ], [ ] 
    total_count, success_count, skipped, no_skipped = 0, 0, 0, 0
     
    for batch_idx, (inputv, cls) in enumerate(trainloader):
        #train loader refers to the training set
        optimizerAttacker.zero_grad() #optimizerAttacker is the UAN attack model
        batch_size = inputv.size(0)
        targets = torch.LongTensor(batch_size)
        if opt.cuda:
            inputv = inputv.cuda()
            targets = targets.cuda()
            cls = cls.cuda()
        inputv = Variable(inputv)
        targets = Variable(targets)
        torch.cuda.empty_cache()
        prediction = netClassifier(inputv) #prediction is the set of data that is predicted by the DenseNet

        # only computer adversarial examples on examples that are originally classified correctly        
        if opt.restrict_to_correct_preds == 1:
            # get indexes where the original predictions are incorrect
            incorrect_idxs = np.array( np.where(prediction.data.max(1)[1].eq(cls).cpu().numpy() == 0))[0].astype(int)
            skipped += incorrect_idxs.shape[0]
            no_skipped += (batch_size - incorrect_idxs.shape[0])
            if incorrect_idxs.shape[0] == batch_size:
                #print("All original predictions were incorrect! Skipping batch!")
                continue
            elif incorrect_idxs.shape[0] > 0 and incorrect_idxs.shape[0] < batch_size:
                # get indexes of the correct predictions and filter out the incorrect indexes
                correct_idxs = np.setdiff1d( np.arange(batch_size), incorrect_idxs)
                correct_idxs = torch.LongTensor(correct_idxs)
                if opt.cuda:
                    correct_idxs = correct_idxs.cuda()
                inputv = torch.index_select(inputv, 0, Variable(correct_idxs)) #inputv updated to now only focus on those correctly predicted by DenseNet
                prediction = torch.index_select(prediction, 0, Variable(correct_idxs)) #only correct predictions by DenseNet is kept in prediction
                cls = torch.index_select(cls, 0, correct_idxs)

        # if this is a targeted attack, fill the target variable and filter out examples that are of that target class 
        if opt.targeted == 1:
            targets.data.resize_as_(cls).fill_(opt.chosen_target_class) 
            ids = np.array( np.where(targets.data.eq(cls).cpu().numpy() == 0))[0].astype(int)
            ids = torch.LongTensor(ids)
            if opt.cuda:
                ids = ids.cuda()
            inputv = torch.index_select(inputv, 0, Variable(ids))
            prediction = torch.index_select(prediction, 0, Variable(ids))
            cls = torch.index_select(cls, 0, ids)

        # update sizes
        batch_size = inputv.size(0)
        with torch.no_grad():
            noise.resize_(batch_size, opt.nz, 1, 1).normal_(0, 0.5)
        with torch.no_grad():
            targets.resize_(batch_size)
       
        # compute an adversarial example and its prediction 
        prediction = netClassifier(inputv)
        netClassifier.eval()
        netAttacker.eval()
        delta = netAttacker(noise)
        adv_sample_ = delta*c + inputv
        adv_sample = torch.clamp(adv_sample_, min_val, max_val) 
        adv_prediction = netClassifier(adv_sample) #adversarial UAN prediction using DenseNet?------------------------------------------------
        
        # get indexes of failed adversarial examples, and store in no_idx
        if opt.targeted == 1:
            no_idx = np.array( np.where(adv_prediction.data.max(1)[1].eq(targets.data).cpu().numpy() == 0))[0].astype(int)
        else:
            no_idx = np.array( np.where(adv_prediction.data.max(1)[1].eq(prediction.data.max(1)[1]).cpu().numpy() == 1))[0].astype(int)

        # update success and total counts         
        success_count += inputv.size(0) - len(no_idx) #success count refers to the number of images successfully perturbed
        total_count += inputv.size(0)     #total count measures the number of images tested 

        # if there are any adversarial examples, compute distance and update norms, and save image
        if len(no_idx) != inputv.size(0):
            yes_idx = np.setdiff1d(np.array(range(inputv.size(0))), no_idx) #yes_idx is those adversarial examples who have successfully fooled DenseNet
            for i, adv_idx in enumerate(yes_idx):
                print(Fore.LIGHTGREEN_EX + 'In training for those UAN successfully fooled DenseNet:  ' + str(i) + str(adv_idx) + ' of batch ' + str(batch_idx)) #code
                clean = inputv[adv_idx].data.view(1, nc, opt.imageSize ,opt.imageSize) #clean image
                adv = adv_sample[adv_idx].data.view(1, nc, opt.imageSize, opt.imageSize) #perturbed image
                pert = (inputv[adv_idx]-adv_sample[adv_idx]).data.view(1, nc, opt.imageSize, opt.imageSize)  #UAN vector = clean - perturbed image 
 
                adv_ = rescale(adv_sample[adv_idx], mean=(0.4914, 0.4822, 0.4465), std=(0.2023, 0.1994, 0.2010))
                clean_ = rescale(inputv[adv_idx], mean=(0.4914, 0.4822, 0.4465), std=(0.2023, 0.1994, 0.2010))
                
                linf = torch.max(torch.abs(adv_ - clean_)).data.cpu().numpy() #linf = perturbed image - clean image
                noise_norm = torch.sqrt(torch.sum( (clean_[:, :, :] - adv_[:, :, :])**2  )).data.cpu().numpy()
                image_norm = torch.sqrt(torch.sum( clean_[:, :, :]**2 )).data.cpu().numpy()
                adv_norm_s   = torch.sqrt(torch.sum( adv_[:, :, :]**2 )).data.cpu().numpy()
                
                dist.append(noise_norm/image_norm)
                pert_norm.append(noise_norm)
                non_adv_norm.append(image_norm)
                adv_norm.append(adv_norm_s)
                L_inf.append(linf)

                if batch_idx == 0: #batch_idx of trainset - why only 1 batch id? Code suggestion: Remove this, save for all batches
                    #only for training set, consider using similar method for test--------------------------------------------
                    vutils.save_image(torch.cat((clean,pert,adv)), './{}/{}_{}.png'.format(opt.outf, epoch, i), normalize=True, scale_each=True)
                    #could only 1 i being produced mean only 1 image is successfully fooled?

        # if opt.optimize_on_success == 0, we do not optimize on already successfully computed adversarial examples
        # we remove them from consideration 
        if opt.optimize_on_success == 0:
            if len(no_idx)!=0:  
                # select the non adv examples to optimise on 
                no_idx = torch.LongTensor(no_idx)
                if opt.cuda:
                    no_idx = no_idx.cuda()
                no_idx = Variable(no_idx)
                #updating all values for those not successfully fooled
                inputv = torch.index_select(inputv, 0, no_idx)
                prediction = torch.index_select(prediction, 0, no_idx)
                targets = torch.index_select(targets, 0, no_idx)
                adv_prediction = torch.index_select(adv_prediction, 0, no_idx)
                delta = torch.index_select(delta, 0, no_idx)
                adv_sample = torch.index_select(adv_sample, 0, no_idx)

        # if opt.optimize_on_success == 1, we continue to optimize on already successfully computed adversarial examples
        # by maximizing the distance between the adversarially predicted class and the target class 
        elif opt.optimize_on_success == 1:
            yes_idx = np.setdiff1d(np.arange(batch_size), no_idx)
            if yes_idx.shape[0]!=0:
                adv_prediction_succ = adv_prediction[torch.LongTensor(yes_idx).cuda()]
                prediction_succ = prediction[torch.LongTensor(yes_idx).cuda()].data.max(1)[1]
                adv_prediction_succ = F.softmax(adv_prediction_succ)
                if no_idx.shape[0]!=0:
                    adv_prediction = adv_prediction[torch.LongTensor(no_idx).cuda()]
                adv_pred_idx = torch.FloatTensor([x[prediction_succ[i]].data[0] for i, x in enumerate(adv_prediction_succ)]).cuda()
                adv_max_idx = adv_prediction_succ.data.max(1)[0]
                success_loss = -torch.mean( torch.log(adv_max_idx)-torch.log(adv_pred_idx) )
            else:
                success_loss = 0

        if len(no_idx)!=0:
            # compute loss and backprop
            adv_prediction_softmax = F.softmax(adv_prediction) #adv_predictions are predictions using DenseNet on those that failed to fool
            #adv_prediction_np = adv_prediction.data.cpu().numpy()
            adv_prediction_np = adv_prediction_softmax.data.cpu().numpy()
            curr_adv_label = Variable(torch.LongTensor( np.array( [arr.argsort()[-1] for arr in adv_prediction_np] ) ) )
            #prediction labels?---------------------------------------------------------------------------   
            if opt.targeted == 1:
                targ_adv_label = Variable(torch.LongTensor( np.array( [targets.data[i] for i, arr in enumerate(adv_prediction_np)] ) ) )
            else:
                targ_adv_label = Variable(torch.LongTensor( np.array( [arr.argsort()[-2] for arr in adv_prediction_np] ) ) )
            if opt.cuda:
                curr_adv_label = curr_adv_label.cuda()
                targ_adv_label = targ_adv_label.cuda()
            curr_adv_pred = adv_prediction_softmax.gather(1, curr_adv_label.unsqueeze(1))
            targ_adv_pred = adv_prediction_softmax.gather(1, targ_adv_label.unsqueeze(1))
            if opt.optimize_on_success == 1:
                 classifier_loss = torch.mean( torch.log(curr_adv_pred)-torch.log(targ_adv_pred) ) + success_loss
            else:
                 classifier_loss = torch.mean( torch.log(curr_adv_pred)-torch.log(targ_adv_pred) )
                 #classifier_loss = size of those not successfully fooled

            if opt.norm == 'linf':
                ldist_loss = opt.ldist_weight*torch.max(torch.abs(adv_sample - inputv)) #ldist loss = adversarial image - origianl image
            elif opt.norm == 'l2':
                ldist_loss = opt.ldist_weight*torch.mean(torch.sqrt(torch.sum( (adv_sample - inputv)**2  )))
            else:
                print("Please define a norm (l2 or linf)")
                exit()
            loss = classifier_loss + ldist_loss 
            loss.backward()
            optimizerAttacker.step()
            c_loss.append(classifier_loss.data.item())
        else:
            if opt.optimize_on_success == 1:
                classifier_loss = success_loss
                c_loss.append(classifier_loss)
                classifier_loss = torch.FloatTensor([classifier_loss])
                classifier_loss = Variable(classifier_loss, requires_grad=True)
                if opt.cuda:
                    classifier_loss = classifier_loss.cuda()
                loss.backward()
                optimizerAttacker.step()
            else:
                c_loss.append(0)
            
        # log to file,  saving for each batch
        progress_bar(batch_idx, len(trainloader), "Tr E%s, C_L %.5f A_Succ %.5f L_inf %.5f L2 %.5f (Pert %.2f, Adv %.2f, Clean %.2f) C %.6f Skipped %.1f%%" %(epoch, np.mean(c_loss), success_count/total_count, np.mean(L_inf), np.mean(dist), np.mean(pert_norm), np.mean(adv_norm), np.mean(non_adv_norm), c, 100*(skipped/(skipped+no_skipped)))) 
        #batch id, length of trainset, epoch, classifier loss of those not fooled (not successful), % successfully perturbed, loss of those successfully fooled, -distance, -pert norm, -adv norm, -non_adv norm, c (scale of perturbation), skipped % where the original predictions are incorrect (attack not done)
        WriteToFile('./%s/log' %(opt.outf),  "Tr Epoch %s batch_idx %s C_L %.5f A_Succ %.5f L_inf %.5f L2 %.5f (Pert %.2f, Adv %.2f, Clean %.2f) C %.6f Skipped %.1f%%" %(epoch, batch_idx, np.mean(c_loss), success_count/total_count, np.mean(L_inf), np.mean(dist), np.mean(pert_norm), np.mean(adv_norm), np.mean(non_adv_norm), c, 100*(skipped/(skipped+no_skipped))))

    # save attack model weights with its epoch 
    if epoch % opt.every == 0:
        torch.save(netAttacker.state_dict(), '%s/netAttacker_%s.pth' % (opt.outf, epoch))

    return success_count/total_count, np.mean(L_inf), np.mean(dist)
    # % successfully perturbed, loss of those successfully fooled, -distance

def test(epoch, c, noise):
    netAttacker.eval()
    netClassifier.eval()
    L_inf = [ ]
    L2 = [ ]
    pert_norm = [ ]
    dist = [ ]
    adv_norm = [ ]
    non_adv_norm = [ ]
    total_count = 0
    success_count = 0
    skipped = 0
    no_skipped = 0
    for batch_idx, (inputv, cls) in enumerate(testloader):
        if opt.cuda:
            inputv = inputv.cuda()
        inputv = Variable(inputv)
        batch_size = inputv.size(0)
 
        targets = torch.LongTensor(batch_size)
        if opt.cuda:
            targets = targets.cuda()
            cls = cls.cuda()
        targets = Variable(targets)
        
        prediction = netClassifier(inputv) #prediction is the set of data that is predicted by the DenseNet
        
        # only computer adversarial examples on examples that are originally classified correctly 
        if opt.restrict_to_correct_preds == 1:
            # get indexes where the original predictions are incorrect
            incorrect_idxs = np.array( np.where(prediction.data.max(1)[1].eq(cls).cpu().numpy() == 0))[0].astype(int)
            skipped += incorrect_idxs.shape[0]
            no_skipped += (batch_size - incorrect_idxs.shape[0])
            if incorrect_idxs.shape[0] == batch_size:
                print("All original predictions were incorrect! Skipping batch!")
                continue
            elif incorrect_idxs.shape[0] > 0 and incorrect_idxs.shape[0] < batch_size:
                # get indexes of the correct predictions and filter out the incorrect indexes
                correct_idxs = np.setdiff1d( np.arange(batch_size), incorrect_idxs)
                correct_idxs = torch.LongTensor(correct_idxs)
                if opt.cuda:
                    correct_idxs = correct_idxs.cuda()
                inputv = torch.index_select(inputv, 0, Variable(correct_idxs)) #inputv updated to now only focus on those correctly predicted by DenseNet
                prediction = torch.index_select(prediction, 0, Variable(correct_idxs)) #only correct predictions by DenseNet is kept in prediction
                cls = torch.index_select(cls, 0, correct_idxs)
        # remove samples that are of the target class
        if opt.targeted == 1:
            targets.data.resize_as_(cls).fill_(opt.chosen_target_class) 
            ids = np.array( np.where(targets.data.eq(cls).cpu().numpy() == 0))[0].astype(int)
            ids = torch.LongTensor(ids)
            if opt.cuda:
                ids = ids.cuda()
            inputv = torch.index_select(inputv, 0, Variable(ids))
            prediction = torch.index_select(prediction, 0, Variable(ids))
            cls = torch.index_select(cls, 0, ids)

        batch_size = inputv.size(0)
        with torch.no_grad():
            noise.resize_(batch_size, opt.nz, 1, 1).normal_(0, 0.5)
        with torch.no_grad():
            targets.resize_(batch_size)
        
         # compute an adversarial example and its prediction
        prediction = netClassifier(inputv)
        delta = netAttacker(noise)
        adv_sample_ = delta*c + inputv
        adv_sample = torch.clamp(adv_sample_, min_val, max_val) 
        adv_prediction = netClassifier(adv_sample) #adversarial UAN prediction using DenseNet?------------------------------------------------
        
        # get indexes of failed adversarial examples, and store in no_idx
        if opt.targeted == 1:
            no_idx = np.array( np.where(adv_prediction.data.max(1)[1].eq(targets.data).cpu().numpy() == 0))[0].astype(int)
        else:
            no_idx = np.array( np.where(adv_prediction.data.max(1)[1].eq(prediction.data.max(1)[1]).cpu().numpy() == 1))[0].astype(int)

        # update success and total counts
        success_count += inputv.size(0) - len(no_idx)
        total_count += inputv.size(0)

        # if there are any adversarial examples, compute distance and update norms, and save image   
        if len(no_idx) != inputv.size(0):
            yes_idx = np.setdiff1d(np.array(range(inputv.size(0))), no_idx) #yes_idx is those adversarial examples who have successfully fooled DenseNet
            for i, adv_idx in enumerate(yes_idx):
                print(Fore.LIGHTGREEN_EX + 'In test for those UAN successfully fooled DenseNet:  ' + str(i) + str(adv_idx) + ' of batch ' + str(batch_idx)) #code
                clean = inputv[adv_idx].data.view(1, nc, opt.imageSize ,opt.imageSize) #clean image
                adv = adv_sample[adv_idx].data.view(1, nc, opt.imageSize, opt.imageSize) #perturbed image
                pert = (inputv[adv_idx]-adv_sample[adv_idx]).data.view(1, nc, opt.imageSize, opt.imageSize)  #UAN vector = clean - perturbed image 

                adv_ = rescale(adv_sample[adv_idx], mean=(0.4914, 0.4822, 0.4465), std=(0.2023, 0.1994, 0.2010))
                clean_ = rescale(inputv[adv_idx], mean=(0.4914, 0.4822, 0.4465), std=(0.2023, 0.1994, 0.2010))
                
                linf = torch.max(torch.abs(adv_ - clean_)).data.cpu().numpy() #linf = perturbed image - clean image
                noise_norm = torch.sqrt(torch.sum( (clean_[:, :, :] - adv_[:, :, :])**2  )).data.cpu().numpy()
                image_norm = torch.sqrt(torch.sum( clean_[:, :, :]**2 )).data.cpu().numpy()
                adv_norm_s   = torch.sqrt(torch.sum( adv_[:, :, :]**2 )).data.cpu().numpy()
                
                dist.append(noise_norm/image_norm)
                pert_norm.append(noise_norm)
                non_adv_norm.append(image_norm)
                adv_norm.append(adv_norm_s)
                L_inf.append(linf)
                
                if batch_idx <= 20:
                    #for i, adv_idx in enumerate(batch_idx):
                        #try to get predictions of clean ------------------------------------
            
                    prediction = netClassifier(clean)
                    print(Fore.LIGHTCYAN_EX + 'clean prediction: ' + str(prediction) + ' --------------------------------')
                    vutils.save_image(clean, './{}/{}_{}_clean.png'.format('classifications', batch_idx, i), normalize=True, scale_each=True)
                    WriteToFile('./%s/classifications' %(opt.outf),  " Original prediction of clean for batch idx %s : %s, with predicted class %s" %(batch_idx, prediction,prediction.data.max(1)[1]))
                    
                    #try to get prediction of perturbed ----------------------------------
                    adv_prediction = netClassifier(adv)
                    print(Fore.LIGHTRED_EX + 'perturbed prediction: ' + str(adv_prediction) + ' --------------------------------')
                    vutils.save_image(adv, './{}/{}_{}_perturbed.png'.format('classifications', batch_idx, i), normalize=True, scale_each=True)
                    WriteToFile('./%s/classifications' %(opt.outf),  "Perturbed prediction of perturbed image for batch idx %s : %s with predicted class %s" %(batch_idx, adv_prediction, adv_prediction.data.max(1)[1]))
                    vutils.save_image(torch.cat((clean,pert,adv)), './{}/{}_{}combined.png'.format('classifications', batch_idx, i), normalize=True, scale_each=True)
                
        #no image saved here, unlike train ---------------------------------------------------------------------------------------------------------        
        progress_bar(batch_idx, len(testloader), "Val E%s, A_Succ %.5f L_inf %.5f L2 %.5f (Pert %.2f, Adv %.2f, Clean %.2f) C %.6f Skipped %.1f%%" %(epoch, success_count/total_count, np.mean(L_inf), np.mean(dist), np.mean(pert_norm), np.mean(adv_norm), np.mean(non_adv_norm), c, 100*(skipped/(skipped+no_skipped)))) 
        #batch id, length of testset, epoch, % successfully perturbed, loss of those successfully fooled, -distance, -pert norm, -adv norm, -non_adv norm, c (scale of perturbation), skipped % where the original predictions are incorrect (attack not done) should not be skipped for test
        WriteToFile('./%s/log' %(opt.outf),  "Val Epoch %s batch_idx %s A_Succ %.5f L_inf %.5f L2 %.5f (Pert %.2f, Adv %.2f, Clean %.2f) C %.6f Skipped %.1f%%" %(epoch, batch_idx, success_count/total_count, np.mean(L_inf), np.mean(dist), np.mean(pert_norm), np.mean(adv_norm), np.mean(non_adv_norm), c, 100*(skipped/(skipped+no_skipped))))


if __name__ == '__main__':

    c = opt.shrink
    min_val, max_val = find_boundaries(trainloader)
    print(min_val, max_val)
    for epoch in range(1, opt.epochs + 1):
        print('epoch: ' + str(epoch))
        start = time.time()
        score, linf, l2 = train(epoch, c, noise)
        # % successfully perturbed, loss of those successfully fooled, -distance
        if linf > opt.max_norm:
            print(Fore.LIGHTCYAN_EX + 'debug: loss > max allowed perturbation in train -----------------')
            break
        if l2 > opt.max_norm:
            print(Fore.LIGHTCYAN_EX + 'debug: distance > max allowed perturbation in train -----------------')
            break
        end = time.time()
        if score >= 1.00:
            print(Fore.LIGHTCYAN_EX + 'debug: all successfully perturbed in train ---------------------------')
            break
        if epoch == 1:
            curr_pred = score
        if epoch % 2 == 0:
            prev_pred = curr_pred
            curr_pred = score
        if epoch > 2:
            if ( prev_pred - curr_pred ) >= 0:
                c += opt.shrink_inc
    test(epoch, c, noise)
