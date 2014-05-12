# -*- coding: utf-8 -*-
'''
Implements a correlated topic model, similar to that described by Blei
but using the Bouchard product of sigmoid bounds instead of Laplace
approximation.

Created on 17 Jan 2014

@author: bryanfeeney
'''

import os # Configuration for PyxImport later on. Requires GCC
os.environ['CC']  = os.environ['HOME'] + "/bin/cc"

from math import log
from math import pi
from math import e

import time

from collections import namedtuple
import numpy as np
import scipy.linalg as la
import scipy.sparse as ssp
import numpy.random as rd
import sys

import model.lda_cvb_fast as compiled

from util.array_utils import normalizerows_ip
from util.sigmoid_utils import rowwise_softmax, scaledSelfSoftDot
from util.sparse_elementwise import sparseScalarQuotientOfDot, \
    sparseScalarProductOfSafeLnDot

# ==============================================================
# CONSTANTS
# ==============================================================

DTYPE=np.float32 # A default, generally we should specify this in the model setup

LN_OF_2_PI   = log(2 * pi)
LN_OF_2_PI_E = log(2 * pi * e)

DEBUG=False

MODEL_NAME="lda/vb/c0"

# ==============================================================
# TUPLES
# ==============================================================

TrainPlan = namedtuple ( \
    'TrainPlan',
    'iterations epsilon logFrequency fastButInaccurate debug')                            

QueryState = namedtuple ( \
    'QueryState', \
    'W_list docLens n_dk n_kt n_k z_dnk'\
)

ModelState = namedtuple ( \
    'ModelState', \
    'K topicPrior vocabPrior n_dk n_kt n_k dtype name'
)

# ==============================================================
# PUBLIC API
# ==============================================================

def newModelFromExisting(model):
    '''
    Creates a _deep_ copy of the given model
    '''
    return ModelState(\
        model.K, \
        model.topicPrior.copy(), \
        model.vocabPrior.copy(), \
        model.n_dk.copy(), \
        model.n_kt.copy(), \
        model.n_k.copy(),  \
        model.dtype,       \
        model.name)

def newModelAtRandom(W, K, topicPrior=None, vocabPrior=None, dtype=DTYPE):
    '''
    Creates a new LDA ModelState for the given training set and
    the given number of topics. Everything is instantiated purely
    at random. This contains all parameters independent of of
    the dataset (e.g. learnt priors)
    
    Param:
    W - the DxT document-term matrix of T terms in D documents
        which will be used for training.
    K - the number of topics
    topicPrior - the prior over topics, either a scalar or a K-dimensional vector
    vocabPrior - the prior over vocabs, either a scalar or a T-dimensional vector
    dtype      - the datatype to be used throughout.
    
    Return:
    A ModelState object
    '''
    assert K > 1, "There must be at least two topics"
    
    if topicPrior is None:
        topicPrior = 50.0 / K # From Griffiths and Steyvers 2004
    if vocabPrior is None:
        vocabPrior = 0.01 # Also from G&S
        
    n_dk = None # These start out at none until we actually
    n_kv = None # go ahead and train this model.
    n_k  = None
    
    return ModelState(K, topicPrior, vocabPrior, n_dk, n_kv, n_k, dtype, MODEL_NAME)


def newQueryState(W, modelState):
    '''
    Creates a new LDA QueryState object. This contains all
    parameters and random variables tied to individual
    datapoints.
    
    Param:
    W - the DxT document-term matrix used for training or
        querying.
    modelState - the model state object
    
    REturn:
    A CtmQueryState object
    '''
    K =  modelState.K
    
    D,_ = W.shape
    W_list, docLens = toWordList(W)
    maxN = int(np.max(docLens)) # bizarre Numpy 1.7 bug in rd.dirichlet/reshape
    
    # Initialise the per-token assignments at random according to the dirichlet hyper
    prior = np.full((K,), modelState.topicPrior)
    z_dnk = rd.dirichlet(prior, size=D * maxN) \
          .astype(modelState.dtype) \
          .reshape((D,maxN,K))
    
    # The following method zeros z[d,n,k] where no word n > word-count for document
    # d emulated a jagged array. It also
    #
    n_dk, n_kt, n_k = compiled.calculateCounts (W_list, docLens, z_dnk, W.shape[1])
    
    # Lastly, convert the memory-views returned from Cython into numpy arrays
    W_list, docLens = np.asarray(W_list), np.asarray(docLens)
    n_dk = np.asarray(n_dk)
    n_kt = np.asarray(n_kt)
    n_k  = np.asarray(n_k)
    
    return QueryState(W_list, docLens, n_dk, n_kt, n_k, z_dnk)


def toWordList (w_csr):
    docLens = np.squeeze(np.asarray(w_csr.sum(axis=1))).astype(np.int32)
    
    if w_csr.dtype == np.int32:
        return compiled.toWordList_i32 (w_csr.indptr, w_csr.indices, w_csr.data, docLens), docLens
    elif w_csr.dtype == np.float32:
        return compiled.toWordList_f32 (w_csr.indptr, w_csr.indices, w_csr.data, docLens), docLens
    elif w_csr.dtype == np.float64:
        return compiled.toWordList_f64 (w_csr.indptr, w_csr.indices, w_csr.data, docLens), docLens
    else:
        raise ValueError("No implementation defined for dtype = " + str(w_csr.dtype))

def newTrainPlan(iterations=100, epsilon=0.01, logFrequency=10, fastButInaccurate=False, debug=DEBUG):
    '''
    Create a training plan determining how many iterations we
    process, how often we plot the results, how often we log
    the variational bound, etc.
    '''
    return TrainPlan(iterations, epsilon, logFrequency, fastButInaccurate, debug)

def train (W, X, modelState, queryState, trainPlan, query=False):
    '''
    Infers the topic distributions in general, and specifically for
    each individual datapoint.

    Params:
    W - the DxT document-term matrix
    X - The DxF document-feature matrix, which is IGNORED in this case
    modelState - the actual LDA model. In a training run (query = False) this
                 will be mutated in place, and then returned.
    queryState - the query results - essentially all the "local" variables
                 matched to the given observations. This will be mutated in-place
                 and then returned.
    trainPlan  - how to execute the training process (e.g. iterations,
                 log-interval etc.)
    query      - 

    Return:
    The updated model object (note parameters are updated in place, so make a 
    defensive copy if you want it)
    The query object with the update query parameters
    '''
    iterations, epsilon, logFrequency, fastButInaccurate, debug = \
        trainPlan.iterations, trainPlan.epsilon, trainPlan.logFrequency, trainPlan.fastButInaccurate, trainPlan.debug           
    W_list, docLens, q_n_dk, q_n_kt, q_n_k, z_dnk = \
        queryState.W_list, queryState.docLens, queryState.n_dk, queryState.n_kt, queryState.n_k, queryState.z_dnk
    K, topicPrior, vocabPrior, m_n_dk, m_n_kt, m_n_k = \
        modelState.K, modelState.topicPrior, modelState.vocabPrior, modelState.n_dk, modelState.n_kt, modelState.n_k
    
    D_train = 0 if m_n_dk is None else m_n_dk.shape[0]
    D_query = q_n_dk.shape[0]
    T = W.shape[1]
    
    # Add the model counts (essentially the learnt model parameters) to those for
    # the query, assuming the model has been trained previously
    if m_n_dk is not None:
        np.add (q_n_kt, m_n_kt, out=q_n_kt) # q_n_kt += m_n_kt
        np.add (q_n_k,  m_n_k,  out=q_n_k)  # q_n_k  += m_n_k
    
    # Now iterate
    if modelState.dtype == np.float32:
        compiled.iterate_f32 (iterations, D_query, D_train, K, T, \
                              W_list, docLens, \
                              q_n_dk, q_n_kt, q_n_k, z_dnk,\
                              topicPrior, vocabPrior)
    elif modelState.dtype == np.float64:
        compiled.iterate_f64 (iterations, D_query, D_train, K, T, \
                              W_list, docLens, \
                              q_n_dk, q_n_kt, q_n_k, z_dnk,\
                              topicPrior, vocabPrior)
    else:
        raise ValueError("No implementation defined for dtype " + str(modelState.dtype))
    
    # Now return the results
    if query: # Model is unchanged, query is changed
        if m_n_dk is not None:
            np.subtract(q_n_kt, m_n_kt, out=q_n_kt) # q_n_kt -= m_n_kt
            np.subtract(q_n_k,  m_n_k,  out=q_n_k)  # q_n_k  -= m_n_k
    else: # train # Model is changed (or flat-out created). Query is changed
        if m_n_dk is not None: # Amend existing
            m_n_dk = np.vstack((m_n_dk, q_n_dk))
            m_n_kt[:,:] = q_n_kt
            m_n_k[:]    = q_n_k
        else:                  # Create from scratch
            m_n_dk = q_n_dk.copy()
            m_n_kt = q_n_kt.copy()
            m_n_k  = q_n_k.copy()
            
    return ModelState(K, topicPrior, vocabPrior, m_n_dk, m_n_kt, m_n_k, modelState.dtype, modelState.name), \
           QueryState(W_list, docLens, q_n_dk, q_n_kt, q_n_k, z_dnk), \
           (np.zeros(1), np.zeros(1), np.zeros(1))
#           iteration    var_bound    ln_likely
    

def query(W, X, modelState, queryState, queryPlan):
    '''
    Given a _trained_ model, attempts to predict the topics for each of
    the inputs.

    Params:
    W - The query words to which we assign topics
    X - This is ignored, and can be omitted
    modelState - the _trained_ model
    queryState - the query state generated for the query dataset
    queryPlan  - used in this case as we need to tighten up the approx

    Returns:
    The model state and query state, in that order. The model state is
    unchanged, the query is.
    '''
    modelState, queryState, _ = train(W, X, modelState, queryState, queryPlan, query=True)
    return modelState, queryState


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
    n_dk, n_kt = queryState.n_dk, modelState.n_kt
    
    # Scale to create distributions over doc-topics and topic-vocabs
    doc_norm = n_dk.sum(axis = 1)
    voc_norm = n_kt.sum(axis = 1)
    
    n_dk /= doc_norm[:,np.newaxis]
    n_kt /= voc_norm[:,np.newaxis]
    
    # Use distributions to create log-likelihood. This could be made
    # faster still by not materializing the (admittedly sparse) matrix
    ln_likely = sparseScalarProductOfSafeLnDot(W, n_dk, n_kt).sum()
    
    # Rescale back to word-counts
    n_dk *= doc_norm
    n_kt *= voc_norm
    
    return ln_likely
    

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

    return bound

def vocab(modelState):
    '''
    Return the vocabulary inferred by this model
    '''
    return modelState.n_dk / (modelState.n_dk.sum(axis=1))[:,np.newaxis]



# ==============================================================
# PUBLIC HELPERS
# ==============================================================

def printStderr(msg):
    sys.stdout.flush()
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()


def static_var(varname, value):
    def decorate(func):
        setattr(func, varname, value)
        return func
    return decorate

@static_var("old_bound", 0)
def _debug_with_bound (itr, var_value, var_name, W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n):
    if np.isnan(var_value).any():
        printStderr ("WARNING: " + var_name + " contains NaNs")
    if np.isinf(var_value).any():
        printStderr ("WARNING: " + var_name + " contains INFs")
    if var_value.dtype != dtype:
        printStderr ("WARNING: dtype(" + var_name + ") = " + str(var_value.dtype))

    old_bound = _debug_with_bound.old_bound
    bound     = var_bound(W, ModelState(K, topicMean, sigT, vocab, A, dtype, MODEL_NAME), QueryState(means, varcs, n))
    diff = "" if old_bound == 0 else "%15.4f" % (bound - old_bound)
    _debug_with_bound.old_bound = bound

    if int(bound - old_bound) < 0:
        printStderr ("Iter %3d Update %-15s Bound %22f (%15s)" % (itr, var_name, bound, diff))
    else:
        print ("Iter %3d Update %-15s Bound %22f (%15s)" % (itr, var_name, bound, diff))

def _debug_with_nothing (itr, var_value, var_name, W, K, topicMean, sigT, vocab, dtype, means, varcs, A, n):
    pass