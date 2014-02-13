# -*- coding: utf-8 -*-
'''
Implements a correlated topic model, similar to that described by Blei
but using the Bouchard product of sigmoid bounds instead of Laplace
approximation.

Created on 17 Jan 2014

@author: bryanfeeney
'''

from math import log
from math import pi
from math import e

from collections import namedtuple
import numpy as np
import scipy.linalg as la
import scipy.sparse as ssp
import scipy.sparse.linalg as sla
import numpy.random as rd
import matplotlib as mpl
#mpl.use('Agg')
import matplotlib.pyplot as plt
import sys

from util.overflow_safe import safe_log, safe_log_one_plus_exp_of, safe_log_det
from util.array_utils import normalizerows_ip
from util.sigmoid_utils import rowwise_softmax, selfSoftDot, scaledSelfSoftDot
from util.sparse_elementwise import sparseScalarProductOf, \
    sparseScalarProductOfDot, sparseScalarQuotientOfDot, \
    entropyOfDot, sparseScalarProductOfSafeLnDot
    
# ==============================================================
# CONSTANTS
# ==============================================================

DTYPE=np.float32 # A default, generally we should specify this in the model setup

LN_OF_2_PI   = log(2 * pi)
LN_OF_2_PI_E = log(2 * pi * e)

DEBUG=True

# ==============================================================
# TUPLES
# ==============================================================

TrainPlan = namedtuple ( \
    'TrainPlan',
    'iterations epsilon logFrequency plot plotFile plotIncremental fastButInaccurate')                            

QueryState = namedtuple ( \
    'QueryState', \
    'means varcs docLens'\
)

ModelState = namedtuple ( \
    'ModelState', \
    'K topicMean sigT vocab A dtype'
)

# ==============================================================
# PUBLIC API
# ==============================================================

def newModelFromExisting(model):
    '''
    Creates a _deep_ copy of the given model
    '''
    return ModelState(model.K, model.topicMean.copy(), model.sigT.copy(), model.vocab.copy(), model.dtype)

def newModelAtRandom(W, K, dtype=DTYPE):
    '''
    Creates a new CtmModelState for the given training set and
    the given number of topics. Everything is instantiated purely
    at random. This contains all parameters independent of of
    the dataset (e.g. learnt priors)
    
    Param:
    W - the DxT document-term matrix of T terms in D documents
        which will be used for training.
    K - the number of topics
    
    Return:
    A CtmModelState object
    '''
    assert K > 1, "There must be at least two topics"
    
    _,T = W.shape
    vocab     = normalizerows_ip(rd.random((K,T)).astype(dtype))
    topicMean = rd.random((K,)).astype(dtype)
    topicMean /= np.sum(topicMean)
    
#    isigT = np.eye(K)
#    sigT  = la.inv(isigT)
    sigT  = np.eye(K)
    
    A = np.eye(K) - 1./K
    
    return ModelState(K, topicMean, sigT, vocab, A, dtype)

def newQueryState(W, modelState):
    '''
    Creates a new CTM Query state object. This contains all
    parameters and random variables tied to individual
    datapoints.
    
    Param:
    W - the DxT document-term matrix used for training or
        querying.
    modelState - the model state object
    
    REturn:
    A CtmQueryState object
    '''
    K, vocab, dtype =  modelState.K, modelState.vocab, modelState.dtype
    
    D,T = W.shape
    assert T == vocab.shape[1], "The number of terms in the document-term matrix (" + str(T) + ") differs from that in the model-states vocabulary parameter " + str(vocab.shape[1])
    docLens = np.squeeze(np.asarray(W.sum(axis=1)))
    
    means = normalizerows_ip(rd.random((D,K)).astype(dtype))
    varcs = np.ones((D,K), dtype=dtype)
    
    return QueryState(means, varcs, docLens)


def newTrainPlan(iterations = 100, epsilon=0.01, logFrequency=10, plot=False, plotFile=None, plotIncremental=False, fastButInaccurate=False):
    '''
    Create a training plan determining how many iterations we
    process, how often we plot the results, how often we log
    the variational bound, etc.
    '''
    return TrainPlan(iterations, epsilon, logFrequency, plot, plotFile, plotIncremental, fastButInaccurate)


def train (W, X, modelState, queryState, trainPlan):
    '''
    Infers the topic distributions in general, and specifically for
    each individual datapoint.
    
    Params:
    W - the DxT document-term matrix
    X - The DxF document-feature matrix, which is IGNORED in this case
    modelState - the actual CTM model
    queryState - the query results - essentially all the "local" variables
                 matched to the given observations
    trainPlan  - how to execute the training process (e.g. iterations,
                 log-interval etc.)
                 
    Return:
    A new model object with the updated model (note parameters are
    updated in place, so make a defensive copy if you want it)
    A new query object with the update query parameters
    '''
    def debug_with_bound (iter, var_value, var_name, W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n):
        if np.isnan(var_value).any():
            printStderr ("WARNING: " + var_name + " contains NaNs")
        if np.isinf(var_value).any():
            printStderr ("WARNING: " + var_name + " contains INFs")
        
        print ("Iter %3d Update %s Bound %f" % (iter, var_name, var_bound(W, ModelState(K, topicMean, sigT, vocab, A, dtype), QueryState(means, varcs, n)))) 
    def debug_with_nothing (iter, var_value, var_name, W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n):   
        pass
    
    D,_ = W.shape
    
    # Unpack the the structs, for ease of access and efficiency
    iterations, epsilon, logFrequency, plot, plotFile, plotIncremental, fastButInaccurate = trainPlan.iterations, trainPlan.epsilon, trainPlan.logFrequency, trainPlan.plot, trainPlan.plotFile, trainPlan.plotIncremental, trainPlan.fastButInaccurate
    means, varcs, n = queryState.means, queryState.varcs, queryState.docLens
    K, topicMean, sigT, vocab, A, dtype = modelState.K, modelState.topicMean, modelState.sigT, modelState.vocab, modelState.A, modelState.dtype
    
    # Book-keeping for logs
    boundIters  = np.zeros(shape=(iterations // logFrequency,))
    boundValues = np.zeros(shape=(iterations // logFrequency,))
    bvIdx = 0
    debugFn = debug_with_bound if DEBUG else debug_with_nothing
    
    # Initialize some working variables
    isigT = la.inv(sigT)
    R = W.copy()
    
    priorSigt_diag = np.ndarray(shape=(K,), dtype=dtype)
    priorSigt_diag.fill (0.001)
    
    # Iterate over parameters
    for iter in range(iterations):
        if iter == 47:
            print ("hmm")
        
        # We start with the M-Step, so the parameters are consistent with our
        # initialisation of the RVs when we do the E-Step
        
        # Update the mean and covariance of the prior
        topicMean = means.mean(axis = 0)
        debugFn (iter, topicMean, "topicMean", W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n)
        
        sigT = np.cov(means.T)
        sigT.flat[::K+1] += varcs.mean(axis=0)
        isigT = la.inv(sigT)
        debugFn (iter, sigT, "sigT", W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n)
        
        # Building Blocks - termporarily replaces means with exp(means)
        expMeans = np.exp(means, out=means)
        R = sparseScalarQuotientOfDot(W, expMeans, vocab, out=R)
        V = expMeans * R.dot(vocab.T)
        
        # Update the vocabulary
        vocab *= (R.T.dot(expMeans)).T # Awkward order to maintain sparsity (R is sparse, expMeans is dense)
        vocab = normalizerows_ip(vocab)
        vocab += 1E-300 # Just to ensure that we don't get zero probabilities in the absence of a proper prior
        
        # Reset the means to their original form, and log effect of vocab update
        means = np.log(expMeans, out=expMeans)
        debugFn (iter, vocab, "vocab", W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n)
        
        # And now this is the E-Step, though it's followed by updates for the
        # parameters also that handle the log-sum-exp approximation.
        
        # Update the Means
        vMat   = (2  * s[:,np.newaxis] * lxi - 0.5) * n[:,np.newaxis] + V
        rhsMat = vMat + isigT.dot(topicMean)
        for d in range(D):
            means[d,:] = la.inv(isigT + ssp.diags(n[d] * 2 * lxi[d,:], 0)).dot(rhsMat[d,:])
        debugFn (iter, means, "means", W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n)
        
        # Update the Variances
        varcs = 1./(2 * n[:,np.newaxis] * lxi + isigT.flat[::K+1])
        debugFn (iter, varcs, "varcs", W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n)
        
        # Update the approximation parameters
        lxi = negJakkolaOfDerivedXi(means, varcs, s)
        debugFn (iter, lxi, "lxi", W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n)
        
        # s can sometimes grow unboundedly
        # Follow Bouchard's suggested approach of fixing it at zero
        #
        s = (np.sum(lxi * means, axis=1) + 0.25 * K - 0.5) / np.sum(lxi, axis=1)
        debugFn (iter, s, "s", W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n)
        
        if logFrequency > 0 and iter % logFrequency == 0:
            modelState = ModelState(K, topicMean, sigT, vocab, A, dtype)
            queryState = QueryState(means, varcs, n)
            
            boundValues[bvIdx] = var_bound(W, modelState, queryState)
            boundIters[bvIdx]  = iter
            print ("\nIteration %d: bound %f" % (iter, boundValues[bvIdx]))
            if bvIdx > 0 and  boundValues[bvIdx - 1] > boundValues[bvIdx]:
                printStderr ("ERROR: bound degradation: %f > %f" % (boundValues[bvIdx - 1], boundValues[bvIdx]))
            print ("Means: min=%f, avg=%f, max=%f\n\n" % (means.min(), means.mean(), means.max()))
            bvIdx += 1
            
    if plot:
        plt.plot(boundIters[5:], boundValues[5:])
        plt.xlabel("Iterations")
        plt.ylabel("Variational Bound")
        plt.show()
        
    
    return \
        ModelState(K, topicMean, sigT, vocab, A, dtype), \
        QueryState(means, varcs, n)
    

def perplexity (W, modelState, queryState):
    '''
    Return the perplexity of this model.
    
    Perplexity is a sort of normalized likelihood, applicable to textual
    data. Specifically it's the reciprocal of the geometric mean of the
    likelihoods of each individual word in the corpus.
    '''
    return np.exp (-log_likelihood (W, modelState, queryState) / np.sum(W.data))
    

def log_likelihood (W, modelState, queryState):
    ''' 
    Return the log-likelihood of the given data W according to the model
    and the parameters inferred for the entries in W stored in the 
    queryState object.
    '''
    return np.sum( \
        sparseScalarProductOfSafeLnDot(\
            W, \
            queryState.means, \
            modelState.vocab \
        ).data \
    )
    
def var_bound(W, modelState, queryState):
    '''
    Determines the variational bounds. Values are mutated in place, but are
    reset afterwards to their initial values. So it's safe to call in a serial
    manner.
    '''
    
    # Unpack the the structs, for ease of access and efficiency
    D,_ = W.shape
    means, varcs, docLens = queryState.means, queryState.varcs, queryState.docLens
    K, topicMean, sigT, vocab, A = modelState.K, modelState.topicMean, modelState.sigT, modelState.vocab, modelState.A
    
    # Calculate some implicit  variables
    isigT = la.inv(sigT)
    
    bound = 0
    
    # Distribution over document topics
    bound -= (D*K)/2. * LN_OF_2_PI
    bound -= D/2. * la.det(sigT)
    diff   = means - topicMean[np.newaxis,:]
    bound -= 0.5 * np.sum (diff.dot(isigT) * diff)
    bound -= 0.5 * np.sum(varcs * np.diag(isigT)[np.newaxis,:]) # = -0.5 * sum_d tr(V_d \Sigma^{-1}) when V_d is diagonal only.
       
    # And its entropy
    bound += 0.5 * D * K * LN_OF_2_PI_E + 0.5 * np.sum(np.log(varcs)) 
    
    # Distribution over word-topic assignments
    expMeans = np.exp(means, out=means)
    R = sparseScalarQuotientOfDot(W, expMeans, vocab)  # D x V   [W / TB] is the quotient of the original over the reconstructed doc-term matrix
    V = expMeans * (R.dot(vocab.T)) # D x K
    means = np.log(expMeans, out=expMeans)
    
    bound += np.sum(means * V)
    bound += np.sum(2 * ssp.diags(docLens,0) * means.dot(A) * means)
    bound -= 2. * scaledSelfSoftDot(means, docLens)
    bound -= 0.5 * np.sum(docLens[:,np.newaxis] * V * (np.diag(A))[np.newaxis,:])
    bound += np.sum(docLens * np.log(np.sum(np.exp(means), axis=1)))
    
    # And it's entropy, and the distribution over words
    bound -= np.sum(means * V) 
    bound += np.sum(sparseScalarProductOfSafeLnDot(W, expMeans, vocab).data)
    
    return bound
        
        
        
        

# ==============================================================
# PUBLIC HELPERS
# ==============================================================

def printStderr(msg):
    sys.stdout.flush()
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()
