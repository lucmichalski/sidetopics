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

import time

from collections import namedtuple
import numpy as np
import scipy.linalg as la
import scipy.sparse as ssp
import scipy.special as fns
import numpy.random as rd

from util.array_utils import normalizerows_ip
from util.sigmoid_utils import rowwise_softmax, scaledSelfSoftDot
from util.sparse_elementwise import sparseScalarQuotientOfDot, \
    sparseScalarProductOfSafeLnDot
from util.misc import printStderr, static_var
from util.overflow_safe import safe_log_det
from model.evals import perplexity_from_like
from model.common import DataSet

from math import isnan

    
# ==============================================================
# CONSTANTS
# ==============================================================

DTYPE=np.float32 # A default, generally we should specify this in the model setup

LN_OF_2_PI   = log(2 * pi)
LN_OF_2_PI_E = log(2 * pi * e)

USE_NIW_PRIOR=False
NIW_PSI=0.1             # isotropic prior
NIW_PSEUDO_OBS_MEAN=+2  # set to NIW_NU = K + NIW_NU_STEP # this is called kappa in the code, go figure
NIW_PSEUDO_OBS_VAR=+2   # related to K
NIW_MU=0

VocabPrior = 1.1

DEBUG=False

MODEL_NAME="stm/uv_vecy/bohning"

# ==============================================================
# TUPLES
# ==============================================================

TrainPlan = namedtuple ( \
    'TrainPlan',
    'iterations epsilon logFrequency fastButInaccurate debug')                            

QueryState = namedtuple ( \
    'QueryState', \
    'means expMeans docLens'\
)

ModelState = namedtuple ( \
    'ModelState', \
    'K A U Y V varA tv ltv fv lfv vocab vocabPrior dtype name'
)

# ==============================================================
# PUBLIC API
# ==============================================================

def wordDists(model):
    return model.vocab

def topicDists(query):
    result  = np.exp(query.topicMean - query.topicMean.sum(axis=1))
    result /= result.sum(axis=1)
    return result

def newModelFromExisting(model):
    '''
    Creates a _deep_ copy of the given model
    '''
    def safe_copy(arr):
        return None if arr is None else arr.copy()

    return ModelState(
        model.K,
        safe_copy(model.A),
        safe_copy(model.U),
        safe_copy(model.Y),
        safe_copy(model.V),
        safe_copy(model.varA),
        model.tv,
        model.ltv,
        model.fv,
        model.lfv,
        safe_copy(model.vocab),
        model.vocabPrior,
        model.dtype,
        model.name
    )

def newModelAtRandom(data, K, Q, P, tv=0.001, ltv=0.001, fv=0.001, lfv=0.001, vocabPrior=VocabPrior, dtype=DTYPE):
    '''
    Creates a new CtmModelState for the given training set and
    the given number of topics. Everything is instantiated purely
    at random. This contains all parameters independent of of
    the dataset (e.g. learnt priors)
    
    Param:
    data - the dataset of words, features and links of which only words are used in this model
    K - the number of topics
    
    Return:
    A CtmModelState object
    '''
    assert K > 1, "There must be at least two topics"
    
    _,T = data.words.shape
    _,F = data.feats.shape

    # Pick some random documents as the vocabulary
    vocab = np.ones((K,T), dtype=dtype)
    for k in range(1, K):
        docLenSum = 0
        while docLenSum < 1000:
            randomDoc  = rd.randint(0, data.doc_count, size=1)
            sample_doc = data.words[randomDoc, :]
            vocab[k, sample_doc.indices] += sample_doc.data
            docLenSum += sample_doc.sum()
        vocab[k,:] /= vocab[k,:].sum()

    # stop-word vocab
    vocab[0,:]  = data.words.sum(axis=0)
    vocab[0,:] /= vocab[0,:].sum()

    U = rd.random((F,P))
    Y = rd.random((P,Q))
    V = rd.random((K,Q))
    A = U.dot(Y).dot(V.T)
    varA = None
    
    return ModelState(K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior, dtype, MODEL_NAME)

def newQueryState(data, modelState):
    '''
    Creates a new CTM Query state object. This contains all
    parameters and random variables tied to individual
    datapoints.
    
    Param:
    data - the dataset of words, features and links of which only words are used in this model
    modelState - the model state object
    
    REturn:
    A CtmQueryState object
    '''
    K, vocab, dtype =  modelState.K, modelState.vocab, modelState.dtype
    
    D,T = data.words.shape
    assert T == vocab.shape[1], "The number of terms in the document-term matrix (" + str(T) + ") differs from that in the model-states vocabulary parameter " + str(vocab.shape[1])
    docLens = np.squeeze(np.asarray(data.words.sum(axis=1)))

    base     = normalizerows_ip(rd.random((D,K*2)).astype(dtype))
    means    = base[:,:K]
    expMeans = base[:,K:]
    
    return QueryState(means, expMeans, docLens)


def newTrainPlan(iterations=100, epsilon=2, logFrequency=10, fastButInaccurate=False, debug=DEBUG):
    '''
    Create a training plan determining how many iterations we
    process, how often we plot the results, how often we log
    the variational bound, etc.
    '''
    return TrainPlan(iterations, epsilon, logFrequency, fastButInaccurate, debug)

def train (data, modelState, queryState, trainPlan):
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
    updated in place, so make a defensive copy if you want itr)
    A new query object with the update query parameters
    '''
    W, X = data.words, data.links
    D, T = W.shape
    F    = X.shape[1]
    
    # Unpack the the structs, for ease of access and efficiency
    iterations, epsilon, logFrequency, diagonalPriorCov, debug = trainPlan.iterations, trainPlan.epsilon, trainPlan.logFrequency, trainPlan.fastButInaccurate, trainPlan.debug
    means, expMeans, docLens = queryState.means, queryState.expMeans, queryState.docLens
    K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior, dtype = \
        modelState.K, modelState.A, modelState.U, modelState.Y,  modelState.V, modelState.varA, modelState.tv, modelState.ltv, modelState.fv, modelState.lfv, modelState.vocab, modelState.vocabPrior, modelState.dtype

    # Book-keeping for logs
    boundIters, boundValues, likelyValues = [], [], []
    
    debugFn = _debug_with_bound if debug else _debug_with_nothing
    
    # Initialize some working variables
    R = W.copy()
    if varA is None:
        varA = la.inv(fv * ssp.eye(F) + X.T.dot(X))
    expMeans = np.exp(means - means.max(axis=1)[:,np.newaxis], out=expMeans)
    
    # Iterate over parameters
    for itr in range(iterations):
        
        # We start with the M-Step, so the parameters are consistent with our
        # initialisation of the RVs when we do the E-Step
        
        # Update U, Y, V given A
        V = la.solve(Y.T.dot(U.T).dot(U).dot(Y), A.T.dot(U).dot(Y).T, sym_pos=True, overwrite_a=True, overwrite_b=True).T
        U = la.solve(Y.dot(V.T).dot(V).dot(Y.T), A.dot(V).dot(Y.T).T, sym_pos=True, overwrite_a=True, overwrite_b=True).T
        Y = la.solve()
        debugFn (itr, Y, "Y", W, X, means, expMeans, docLens, K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior)

        A = varA.dot(U.dot(Y).dot(V.T))
        debugFn (itr, A, "A", W, X, means, expMeans, docLens, K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior)
        
        # Building Blocks - temporarily replaces means with exp(means)
        expMeans = np.exp(means - means.max(axis=1)[:,np.newaxis], out=expMeans)
        R = sparseScalarQuotientOfDot(W, expMeans, vocab, out=R)
        
        # Update the vocabulary
        vocab *= (R.T.dot(expMeans)).T # Awkward order to maintain sparsity (R is sparse, expMeans is dense)
        vocab += vocabPrior
        vocab = normalizerows_ip(vocab)
        
        # Reset the means to their original form, and log effect of vocab update
        R = sparseScalarQuotientOfDot(W, expMeans, vocab, out=R)
        V = expMeans * R.dot(vocab.T)

        debugFn (itr, vocab, "vocab", W, X, means, expMeans, docLens, K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior)
        
        # And now this is the E-Step, though itr's followed by updates for the
        # parameters also that handle the log-sum-exp approximation.
        
        # Update the Variances: var_d = (2 N_d * A + isigT)^{-1}
        varcs = np.reciprocal(docLens[:,np.newaxis] * (K-1.)/K + np.diagonal(sigT))
        debugFn (itr, varcs, "varcs", W, X, means, expMeans, docLens, K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior)
        
        # Update the Means
        rhs = V.copy()
        rhs += docLens[:,np.newaxis] * means.dot(A) + isigT.dot(topicMean)
        rhs -= docLens[:,np.newaxis] * rowwise_softmax(means, out=means)
        if diagonalPriorCov:
            means = varcs * rhs
        else:
            for d in range(D):
                means[d, :] = la.inv(isigT + docLens[d] * A).dot(rhs[d, :])
        
#         means -= (means[:,0])[:,np.newaxis]
        
        debugFn (itr, means, "means", W, X, means, expMeans, docLens, K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior)
        
        if logFrequency > 0 and itr % logFrequency == 0:
            modelState = ModelState(K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior, dtype, modelState.name)
            queryState = QueryState(means, expMeans, docLens)
            
            boundValues.append(var_bound(data, modelState, queryState))
            likelyValues.append(log_likelihood(data, modelState, queryState))
            boundIters.append(itr)
            
            print (time.strftime('%X') + " : Iteration %d: bound %f \t Perplexity: %.2f" % (itr, boundValues[-1], perplexity_from_like(likelyValues[-1], docLens.sum())))
            if len(boundValues) > 1:
                if boundValues[-2] > boundValues[-1]:
                    if debug: printStderr ("ERROR: bound degradation: %f > %f" % (boundValues[-2], boundValues[-1]))
        
                # Check to see if the improvement in the bound has fallen below the threshold
                if itr > 100 and len(likelyValues) > 3 \
                    and abs(perplexity_from_like(likelyValues[-1], docLens.sum()) - perplexity_from_like(likelyValues[-2], docLens.sum())) < 1.0:
                    break

    return \
        ModelState(K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior, dtype, modelState.name), \
        QueryState(means, expMeans, docLens), \
        (np.array(boundIters), np.array(boundValues), np.array(likelyValues))

def query(data, modelState, queryState, queryPlan):
    '''
    Given a _trained_ model, attempts to predict the topics for each of
    the inputs.
    
    Params:
    data - the dataset of words, features and links of which only words are used in this model
    modelState - the _trained_ model
    queryState - the query state generated for the query dataset
    queryPlan  - used in this case as we need to tighten up the approx
    
    Returns:
    The model state and query state, in that order. The model state is
    unchanged, the query is.
    '''
    iterations, epsilon, logFrequency, diagonalPriorCov, debug = queryPlan.iterations, queryPlan.epsilon, queryPlan.logFrequency, queryPlan.fastButInaccurate, queryPlan.debug
    means, expMeans, docLens = queryState.means, queryState.expMeans, queryState.docLens
    K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior, dtype = \
        modelState.K, modelState.A, modelState.U, modelState.Y,  modelState.V, modelState.varA, modelState.tv, modelState.ltv, modelState.fv, modelState.lfv, modelState.vocab, modelState.vocabPrior, modelState.dtype

    debugFn = _debug_with_bound if debug else _debug_with_nothing
    W = data.words
    D = W.shape[0]
    
    # Necessary temp variables (notably the count of topic to word assignments
    # per topic per doc)
    isigT = la.inv(sigT)
    
    # Update the Variances
    varcs = 1./((n * (K-1.)/K)[:,np.newaxis] + isigT.flat[::K+1])
    debugFn (0, varcs, "varcs", W, K, topicMean, sigT, vocab, vocabPrior, dtype, means, varcs, A, n)
    
    lastPerp = 1E+300 if dtype is np.float64 else 1E+30
    R = W.copy()
    for itr in range(iterations):
        expMeans = np.exp(means - means.max(axis=1)[:,np.newaxis], out=expMeans)
        R = sparseScalarQuotientOfDot(W, expMeans, vocab, out=R)
        V = expMeans * R.dot(vocab.T)
        
        # Update the Means
        rhs = V.copy()
        rhs += n[:,np.newaxis] * means.dot(A) + isigT.dot(topicMean)
        rhs -= n[:,np.newaxis] * rowwise_softmax(means, out=means)
        if diagonalPriorCov:
            means = varcs * rhs
        else:
            for d in range(D):
                means[d,:] = la.inv(isigT + n[d] * A).dot(rhs[d,:])
        
        debugFn (itr, means, "means", W, K, topicMean, sigT, vocab, vocabPrior, dtype, means, varcs, A, n)
        
        like = log_likelihood(data, modelState, QueryState(means, expMeans, varcs, n))
        perp = perplexity_from_like(like, data.word_count)
        if itr > 20 and lastPerp - perp < 1:
            break
        lastPerp = perp

    return modelState, queryState


def log_likelihood (data, modelState, queryState):
    ''' 
    Return the log-likelihood of the given data W according to the model
    and the parameters inferred for the entries in W stored in the 
    queryState object.
    '''
    return np.sum( \
        sparseScalarProductOfSafeLnDot(\
            data.words, \
            rowwise_softmax(queryState.means), \
            modelState.vocab \
        ).data \
    )
    
def var_bound(data, modelState, queryState):
    '''
    Determines the variational bounds. Values are mutated in place, but are
    reset afterwards to their initial values. So it's safe to call in a serial
    manner.
    '''
    
    # Unpack the the structs, for ease of access and efficiency
    W,X = data.words, data.links
    D,T,F = W.shape[0], W.shape[1], W.shape[2]
    means, expMeans, docLens = queryState.means, queryState.expMeans, queryState.docLens
    K, A, U, Y,  V, varA, tv, ltv, fv, lfv, vocab, vocabPrior, dtype = \
        modelState.K, modelState.A, modelState.U, modelState.Y,  modelState.V, modelState.varA, modelState.tv, modelState.ltv, modelState.fv, modelState.lfv, modelState.vocab, modelState.vocabPrior, modelState.dtype

    # Calculate some implicit  variables
    isigT = la.inv(sigT)
    
    bound = 0
    
    if USE_NIW_PRIOR:
        pseudoObsMeans = K + NIW_PSEUDO_OBS_MEAN
        pseudoObsVar   = K + NIW_PSEUDO_OBS_VAR

        # distribution over topic covariance
        bound -= 0.5 * K * pseudoObsVar * log(NIW_PSI)
        bound -= 0.5 * K * pseudoObsVar * log(2)
        bound -= fns.multigammaln(pseudoObsVar / 2., K)
        bound -= 0.5 * (pseudoObsVar + K - 1) * safe_log_det(sigT)
        bound += 0.5 * NIW_PSI * np.trace(isigT)

        # and its entropy
        # is a constant which we skip
        
        # distribution over means
        bound -= 0.5 * K * log(1./pseudoObsMeans) * safe_log_det(sigT)
        bound -= 0.5 / pseudoObsMeans * (topicMean).T.dot(isigT).dot(topicMean)
        
        # and its entropy
        bound += 0.5 * safe_log_det(sigT) # +  a constant
        
    
    # Distribution over document topics
    bound -= (D*K)/2. * LN_OF_2_PI
    bound -= D/2. * la.det(sigT)
    diff   = means - topicMean[np.newaxis,:]
    bound -= 0.5 * np.sum (diff.dot(isigT) * diff)
    bound -= 0.5 * np.sum(varcs * np.diag(isigT)[np.newaxis,:]) # = -0.5 * sum_d tr(V_d \Sigma^{-1}) when V_d is diagonal only.
       
    # And its entropy
#     bound += 0.5 * D * K * LN_OF_2_PI_E + 0.5 * np.sum(np.log(varcs)) 
    
    # Distribution over word-topic assignments and words and the formers
    # entropy. This is somewhat jumbled to avoid repeatedly taking the
    # exp and log of the means
    expMeans = np.exp(means - means.max(axis=1)[:,np.newaxis], out=expMeans)
    R = sparseScalarQuotientOfDot(W, expMeans, vocab)  # D x V   [W / TB] is the quotient of the original over the reconstructed doc-term matrix
    V = expMeans * (R.dot(vocab.T)) # D x K
    
    bound += np.sum(docLens * np.log(np.sum(expMeans, axis=1)))
    bound += np.sum(sparseScalarProductOfSafeLnDot(W, expMeans, vocab).data)
    
    bound += np.sum(means * V)
    bound += np.sum(2 * ssp.diags(docLens,0) * means.dot(A) * means)
    bound -= 2. * scaledSelfSoftDot(means, docLens)
    bound -= 0.5 * np.sum(docLens[:,np.newaxis] * V * (np.diag(A))[np.newaxis,:])
    
    bound -= np.sum(means * V) 
    
    
    return bound
        

# ==============================================================
# PUBLIC HELPERS
# ==============================================================


@static_var("old_bound", 0)
def _debug_with_bound (itr, var_value, var_name, W, K, topicMean, sigT, vocab, vocabPrior, dtype, means, varcs, A, n):
    if np.isnan(var_value).any():
        printStderr ("WARNING: " + var_name + " contains NaNs")
    if np.isinf(var_value).any():
        printStderr ("WARNING: " + var_name + " contains INFs")
    if var_value.dtype != dtype:
        printStderr ("WARNING: dtype(" + var_name + ") = " + str(var_value.dtype))
    
    old_bound = _debug_with_bound.old_bound
    bound     = var_bound(DataSet(W), ModelState(K, topicMean, sigT, vocab, vocabPrior, A, dtype, MODEL_NAME), QueryState(means, means.copy(), varcs, n))
    diff = "" if old_bound == 0 else "%15.4f" % (bound - old_bound)
    _debug_with_bound.old_bound = bound
    
    addendum = ""
    if var_name == "sigT":
        try:
            addendum = "det(sigT) = %g" % (la.det(sigT))
        except:
            addendum = "det(sigT) = <undefined>"
    
    if isnan(bound):
        printStderr ("Bound is NaN")
    elif int(bound - old_bound) < 0:
        printStderr ("Iter %3d Update %-15s Bound %22f (%15s)     %s" % (itr, var_name, bound, diff, addendum)) 
    else:
        print ("Iter %3d Update %-15s Bound %22f (%15s)     %s" % (itr, var_name, bound, diff, addendum)) 

def _debug_with_nothing (itr, var_value, var_name, W, K, topicMean, sigT, vocab, vocabPrior, dtype, means, varcs, A, n):
    pass

