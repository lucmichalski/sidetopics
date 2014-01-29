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
from util.array_utils import normalizerows_ip, rowwise_softmax
from util.sparse_elementwise import sparseScalarProductOf, \
    sparseScalarProductOfDot, sparseScalarQuotientOfDot, \
    entropyOfDot, sparseScalarProductOfSafeLnDot
import model.ctm as ctm
from model.ctm import printStderr, verifyProper, perplexity, \
    LN_OF_2_PI, LN_OF_2_PI_E, DTYPE
    
# ==============================================================
# CONSTANTS
# ==============================================================

DEBUG=True

# ==============================================================
# TUPLES
# ==============================================================

TrainPlan = namedtuple ( \
    'TrainPlan',
    'iterations epsilon logFrequency plot plotFile plotIncremental fastButInaccurate')                            

QueryState = namedtuple ( \
    'QueryState', \
    'means varcs lxi s docLens'\
)

ModelState = namedtuple ( \
    'ModelState', \
    'F P K A R_A fv Y R_Y lfv V topicMean sigT vocab dtype'
)

# ==============================================================
# PUBLIC API
# ==============================================================

def newModelAtRandom(X, W, P, K, featVar, latFeatVar, dtype=DTYPE):
    '''
    Creates a new CtmModelState for the given training set and
    the given number of topics. Everything is instantiated purely
    at random. This contains all parameters independent of of
    the dataset (e.g. learnt priors)
    
    Param:
    X - The DxF document-feature matrix of F features associated
        with the D documents
    W - the DxT document-term matrix of T terms in D documents
        which will be used for training.
    P - The size of the latent feature-space P << F
    K - the number of topics
    featVar - the prior variance of the feature-space: this is a
              scalar used to scale an identity matrix
    featVar - the prior variance of the latent feature-space: this
               is a scalar used to scale an identity matrix
    
    Return:
    A ModelState object
    '''
    assert K > 1, "There must be at least two topics"
    
    base = ctm.newModelAtRandom(W, K, dtype)
    _,F = X.shape
    Y = rd.random((K,P)).astype(dtype)
    R_Y = latFeatVar * np.eye(P,P)
    
    V = rd.random((P,F)).astype(dtype)
    A = Y.dot(V)
    R_A = featVar * np.eye(F,F)
    
    return ModelState(F, P, K, A, R_A, featVar, Y, R_Y, latFeatVar, V, base.topicMean, base.sigT, base.vocab, dtype)

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
    A QueryState object
    '''
    base = ctm.newQueryState(W, modelState)
    
    return ctm.QueryState(base.means, base.varcs, base.lxi, base.s, base.docLens)


def newTrainPlan(iterations = 100, epsilon=0.01, logFrequency=10, plot=False, plotFile=None, plotIncremental=False, fastButInaccurate=False):
    '''
    Create a training plan determining how many iterations we
    process, how often we plot the results, how often we log
    the variational bound, etc.
    '''
    base = ctm.newTrainPlan(iterations, epsilon, logFrequency, plot, plotFile, plotIncremental, fastButInaccurate)
    return TrainPlan(base.iterations, base.epsilon, base.logFrequency, base.plot, base.plotFile, base.plotIncremental, base.fastButInaccurate)


def train (W, X, modelState, queryState, trainPlan):
    '''
    Infers the topic distributions in general, and specifically for
    each individual datapoint.
    
    Params:
    W - the DxT document-term matrix
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
    def debug_with_bound (iter, var_value, var_name, W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n):
        if np.isnan(var_value).any():
            printStderr ("WARNING: " + var_name + " contains NaNs")
        if np.isinf(var_value).any():
            printStderr ("WARNING: " + var_name + " contains INFs")
        
        print ("Iter %3d Update %s Bound %f" % (iter, var_name, var_bound(W, X, ModelState(F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype), QueryState(means, varcs, lxi, s, n)))) 
    def debug_with_nothing (iter, var_value, var_name, W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n):
        pass
    
    D,_ = W.shape
    
    # Unpack the the structs, for ease of access and efficiency
    iterations, epsilon, logFrequency, plot, plotFile, plotIncremental, fastButInaccurate = trainPlan.iterations, trainPlan.epsilon, trainPlan.logFrequency, trainPlan.plot, trainPlan.plotFile, trainPlan.plotIncremental, trainPlan.fastButInaccurate
    means, varcs, lxi, s, n = queryState.means, queryState.varcs, queryState.lxi, queryState.s, queryState.docLens
    F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype = modelState.F, modelState.P, modelState.K, modelState.A, modelState.R_A, modelState.fv, modelState.Y, modelState.R_Y, modelState.lfv, modelState.V, modelState.topicMean, modelState.sigT, modelState.vocab, modelState.dtype
    
    # Book-keeping for logs
    boundIters  = np.zeros(shape=(iterations // logFrequency,))
    boundValues = np.zeros(shape=(iterations // logFrequency,))
    bvIdx = 0
    debugFn = debug_with_bound if DEBUG else debug_with_nothing
    
    # Initialize some working variables
    isigT = la.inv(sigT)
    R = W.copy()
    
    print("Creating posterior covariance of A, this will take some time...")
    R_A = X.T.dot(X)
    R_A *= 1./lfv
    R_A = R_A.todense()      # dense inverse typical as fast or faster than sparse inverse
    R_A.flat[::F+1] += 1./fv # and the result is usually dense in any rate
    R_A = la.inv(R_A)
    print("Covariance matrix calculated, launching inference")
    
    s.fill(0)
    
    # Iterate over parameters
    for iter in range(iterations):
        
        # We start with the M-Step, so the parameters are consistent with our
        # initialisation of the RVs when we do the E-Step
        
        # Update the mean and covariance of the prior
        topicMean = means.mean(axis = 0)
        debugFn (iter, topicMean, "topicMean", W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n)
        
        sigT = np.cov(means.T)
        sigT.flat[::K+1] += varcs.mean(axis=0)
        isigT = la.inv(sigT)
        debugFn (iter, sigT, "sigT", W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n)
        
        # Building Blocks - termporarily replaces means with exp(means)
        expMeans = np.exp(means, out=means)
        R = sparseScalarQuotientOfDot(W, expMeans, vocab, out=R)
        S = expMeans * R.dot(vocab.T)
        
        # Update the vocabulary
        vocab *= (R.T.dot(expMeans)).T # Awkward order to maintain sparsity (R is sparse, expMeans is dense)
        vocab = normalizerows_ip(vocab)
        vocab += 1E-300
        
        # Reset the means to their original form, and log effect of vocab update
        means = np.log(expMeans, out=expMeans)
        debugFn (iter, vocab, "vocab", W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n)
        
        # And now this is the E-Step, though it's followed by updates for the
        # parameters also that handle the log-sum-exp approximation.
        
        # Update the distribution on the latent space
        
        
        
        # Update the Means
        vMat   = (2  * s[:,np.newaxis] * lxi - 0.5) * n[:,np.newaxis] + S
        rhsMat = vMat + isigT.dot(topicMean)
        for d in range(D):
            means[d,:] = la.inv(isigT + ssp.diags(n[d] * 2 * lxi[d,:], 0)).dot(rhsMat[d,:])
        debugFn (iter, means, "means", W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n)
        
        # Update the Variances
        varcs = 1./(2 * n[:,np.newaxis] * lxi + isigT.flat[::K+1])
        debugFn (iter, varcs, "varcs", W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n)
        
        # Update the approximation parameters
        lxi = ctm.negJakkolaOfDerivedXi(means, varcs, s)
        debugFn (iter, lxi, "lxi", W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n)
        
        # s can sometimes grow unboundedly
        # Follow Bouchard's suggested approach of fixing it at zero
        #
#        s = (np.sum(lxi * means, axis=1) + 0.25 * K - 0.5) / np.sum(lxi, axis=1)
#        debugFn (iter, s, "s", W, X, F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype, means, varcs, lxi, s, n)
        
        if logFrequency > 0 and iter % logFrequency == 0:
            modelState = ModelState(F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype)
            queryState = QueryState(means, varcs, lxi, s, n)
            
            boundValues[bvIdx] = var_bound(W, X, modelState, queryState)
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
        ModelState(F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype), \
        QueryState(means, varcs, lxi, s, n)
    

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
    
def var_bound(W, X, modelState, queryState):
    '''
    Determines the variational bounds. Values are mutated in place, but are
    reset afterwards to their initial values. So it's safe to call in a serial
    manner.
    '''
    
    # Unpack the the structs, for ease of access and efficiency
    D,_ = W.shape
    means, varcs, lxi, s, docLens = queryState.means, queryState.varcs, queryState.lxi, queryState.s, queryState.docLens
    F, P, K, A, R_A, fv, Y, R_Y, lfv, V, topicMean, sigT, vocab, dtype = modelState.F, modelState.P, modelState.K, modelState.A, modelState.R_A, modelState.fv, modelState.Y, modelState.R_Y, modelState.lfv, modelState.V, modelState.topicMean, modelState.sigT, modelState.vocab, modelState.dtype
    
    # Calculate some implicit  variables
    xi = ctm._deriveXi(means, varcs, s)
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
    # This also takes into account all the variables that 
    # constitute the bound on log(sum_j exp(mean_j)) and
    # also incorporates the implicit entropy of Z_dvk
    bound -= np.sum((means*means + varcs*varcs) * docLens[:,np.newaxis] * lxi)
    bound += np.sum(means * 2 * docLens[:,np.newaxis] * s[:,np.newaxis] * lxi)
    bound += np.sum(means * -0.5 * docLens[:,np.newaxis])
    # The last term of line 1 gets cancelled out by part of the first term in line 2
    # so neither are included here.
    
    expMeans = np.exp(means, out=means)
    bound -= -np.sum(sparseScalarProductOfSafeLnDot(W, expMeans, vocab).data)
    
    bound -= np.sum(docLens[:,np.newaxis] * lxi * ((s*s)[:,np.newaxis] - (xi * xi)))
    bound += np.sum(0.5 * docLens[:,np.newaxis] * (s[:,np.newaxis] + xi))
    bound -= np.sum(docLens[:,np.newaxis] * safe_log_one_plus_exp_of(xi))
    
    bound -= np.dot(s, docLens)
    
    means = np.log(expMeans, out=expMeans)
    
    return bound
        
        


