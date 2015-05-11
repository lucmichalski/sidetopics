'''
Created on 15 Apr 2015

@author: bryanfeeney
'''
import sys
import numpy as np
import numpy.random as rd
import scipy.sparse as ssp
import scipy.special as fns
import scipy.linalg as la
import numba as nb

from model.rtm import _links_up_to
from util.sparse_elementwise import sparseScalarProductOfSafeLnDot
from util.overflow_safe import safe_log
from util.misc import constantArray, converged

from collections import namedtuple

from sklearn.decomposition import PCA

from math import log, pi, e

MODEL_NAME = "mtm/vb"
DTYPE      = np.float64

Vagueness = 1E-6 # how vague should the priors over U and V be.

TrainPlan = namedtuple ( \
    'TrainPlan',
    'iterations epsilon logFrequency fastButInaccurate debug')

QueryState = namedtuple ( \
    'QueryState', \
    'docLens topics U V tsums_bydoc tsums_bytop exp_tsums_bydoc exp_tsums_bytop out_counts in_counts'\
)

ModelState = namedtuple ( \
    'ModelState', \
    'K Q topicPrior vocabPrior wordDists topicCov dtype name'
)

def newModelFromExisting(model):
    '''
    Creates a _deep_ copy of the given model
    '''
    return ModelState(
        model.K,
        model.Q,
        model.topicPrior.copy(),
        model.vocabPrior,
        None if model.wordDists is None else model.wordDists.copy(),
        None if model.topicCov is None else model.topicCov,
        model.dtype,
        model.name)


def newModelAtRandom(data, K, Q, topicPrior=None, vocabPrior=None, dtype=DTYPE):
    '''
    Creates a new model using a random initialisation of the given parameters

    :param data: the data to be used in training, should contain words and links
    :param K:  the number of topics to infer
    :param Q:  the number of latent document groups
    :param topicPrior:  the prior over topics, either a scalar oa K-dim vector
    :param vocabPrior:  the prior over words, a scalar
    :param U: the topic matrix is decomposed as topics = V.dot(U.T). Hence U is a Q x K
    matrix
    :param V:  the topic matrix is decomposed as topics = V.dot(U.T). Hence V is a Q x D
    matrix
    :param dtype: the data type used for all fields in this dataset.
    :return: a new ModelState object.
    '''
    assert K > 1,   "There must be at least two topics"
    assert K < 255, "There can be no more than 255 topics"
    assert Q < K,   "By definition the rank of the doc-covariance must be leq K, so Q < K, but you have Q=" + str(Q) + " and K=" + str(K)

    T = data.words.shape[1]

    if topicPrior is None:
        topicPrior = constantArray((K,), 50.0 / K + 0.5, dtype) # From Griffiths and Steyvers 2004
    if vocabPrior is None:
        vocabPrior = 0.1 + 0.5 # Also from G&S

    # Pick some documents at random, and let a vague distribution of their
    # words consitute our initial guess.
    wordDists = np.ones((K, T), dtype=dtype)
    doc_ids = rd.randint(0, data.doc_count, size=K)
    for k in range(K):
        sample_doc = data.words[doc_ids[k], :]
        wordDists[k, sample_doc.indices] += sample_doc.data

    # Scale up so it properly resembles something inferred from this dataset
    # (this avoids catastrophic underflow in softmax)
    wordDists *= data.word_count / K

    return ModelState(K, Q, topicPrior, vocabPrior, wordDists, Vagueness * np.eye((K, K)), dtype=dtype, name=MODEL_NAME)


def newQueryState(data, model):
    '''
    Creates a new QueryState object, containing all the document-level parameters

    :param data:  the data to do inference on, must have links and words
    :param model: the model used to do inference (i.e. global parameter values)
    :return: a new QueryState object
    '''
    D, Q, K = data.doc_count, model.Q, model.K
    docLens = np.squeeze(np.asarray(data.words.sum(axis=1)))

    # Use one large matrix, partitioned into submatrices, to try to make this
    # a little more cache friendly.
    base = np.ndarray(shape=(D, K + Q + 3), dtype=model.dtype, order='C')
    topics      = base[:, :K]
    U           = base[:, K:(K+Q)]
    out_counts  = base[:, K+Q]
    tsums_bydoc = base[:, K+Q + 1]
    exp_tsums_bydoc = base[:, K+Q + 2]

    # Initialise the per-token assignments at random according to the dirichlet hyper
    # This is super-slow
    topics[:] = rd.random((D, K)) + 0.001

    # Use this initialisation to create the factorization topicDists = U V
    pca = PCA(n_components=model.Q)
    pca.fit(topics.T)
    U[:, :] = pca.components_.T
    V       = pca.transform(topics.T).T

    # The sums
    tsums_bydoc[:] = topics.sum(axis=1)
    tsums_bytop    = topics.sum(axis=0)

    np.exp(topics, out=topics)
    exp_tsums_bydoc[:] = topics.sum(axis=1)
    exp_tsums_bytop    = topics.sum(axis=0)
    np.log(topics, out=topics)

    out_counts = docLens + data.links.sum(axis=0)
    in_counts = np.empty((K,), dtype=model.dtype) # Just invent a nonsense value
    in_counts.fill(data.links.sum() / K)

    # Now assign a topic to
    return QueryState(docLens,topics, U, V, tsums_bydoc, tsums_bytop, exp_tsums_bydoc, exp_tsums_bytop, out_counts, in_counts)


def newTrainPlan(iterations=100, epsilon=2, logFrequency=10, fastButInaccurate=False, debug=False):
    '''
    Create a training plan determining how many iterations we
    process, how often we plot the results, how often we log
    the variational bound, etc.

    epsilon is oddly measured, we just evaluate the angle of the line segment between
    the last value of the bound and the current, and if it's less than the given angle,
    then stop.
    '''
    return TrainPlan(iterations, epsilon, logFrequency, fastButInaccurate, debug)


def wordDists (modelState):
    '''
    The K x T matrix of  word distributions inferred for the K topics
    '''
    result = modelState.wordDists.copy()
    norm   = result.sum(axis=1)
    result /= norm[:, np.newaxis]

    return result

def linkDists(queryState):
    '''
    The KxD matrix of per-topic document (i.e. out-link) probabilitieis
    '''
    return topicDists(queryState).T


def topicDists (queryState):
    '''
    The D x K matrix of topics distributions inferred for the K topics
    across all D documents
    '''
    result  = np.exp(queryState.topics - queryState.topics.max(axis=1)[:, np.newaxis])
    norm    = np.sum(result, axis=1)
    result /= norm[:, np.newaxis]

    return result

#@nb.jit
def _log_likelihood_internal(data, model, query):
    result = log_likelihood(data, model, query)

    return result


def log_likelihood (data, modelState, queryState):
    '''
    Return the log-likelihood of the given data W and X according to the model
    and the parameters inferred for the entries in W and X stored in the
    queryState object.

    Actually returns a vector of D document specific log likelihoods
    '''
    wordLikely = sparseScalarProductOfSafeLnDot(data.words, topicDists(queryState), wordDists(modelState)).sum()
    
    # For likelihood it's a bit tricky. In theory, given d =/= p, and letting 
    # c_d = 1/n_d, where n_d is the word count of document d, it's 
    #
    #   ln p(y_dp|weights) = E[\sum_k weights[k] * (c_d \sum_n z_dnk) * (c_p \sum_n z_pnk)]
    #                      = \sum_k weights[k] * c_d * E[\sum_n z_dnk] * c_p * E[\sum_n z_pnk]
    #                      = \sum_k weights[k] * topicDistsMean[d,k] * topicDistsMean[p,k]
    #                      
    #
    # where topicDistsMean[d,k] is the mean of the k-th element of the Dirichlet parameterised
    # by topicDist[d,:]
    #
    # However in the related paper on Supervised LDA, which uses this trick of average z_dnk,
    # they explicitly say that in the likelihood calculation they use the expectation
    # according to the _variational_ approximate posterior distribution q(z_dn) instead of the
    # actual distribution p(z_dn|topicDist), and thus
    #
    # E[\sum_n z_dnk] = \sum_n E_q[z_dnk] 
    #
    # There's no detail of the likelihood in either of the RTM papers, so we use the
    # variational approch
    
    linkLikely = 0
    
    return wordLikely + linkLikely



#@nb.jit
def _inplace_softmax_colwise(z):
    '''
    Softmax transform of the given vector of scores into a vector of
    probabilities. Safe against overflow.

    Transform happens in-place

    :param z: a KxN matrix representing N unnormalised distributions over K
    possibilities, and returns N normalized distributions
    '''
    z_max = z.max(axis=0)
    z -= z_max[np.newaxis, :]

    np.exp(z, out=z)

    z_sum = z.sum(axis=0)
    z /= z_sum[np.newaxis, :]

#@nb.jit
def _inplace_softmax_rowwise(z):
    '''
    Softmax transform of the given vector of scores into a vector of
    probabilities. Safe against overflow.

    Transform happens in-place

    :param z: a NxK matrix representing N unnormalised distributions over K
    possibilities, and returns N normalized distributions
    '''
    z_max = z.max(axis=1)
    z -= z_max[:, np.newaxis]

    np.exp(z, out=z)

    z_sum = z.sum(axis=1)
    z /= z_sum[:, np.newaxis]




#@nb.jit
def _update_topics_at_d(d, W, docLens, topicMeans, topicPrior, diWordDists, diWordDistSums):
    '''
    Infers the topic assignments for all present words in the given document at
    index d as stored in the sparse CSR matrix W. This are used to update the
    topicMeans matrix in-place! The indices of the non-zero words, and their
    probabilities, are returned.
    :param d:  the document for which we update the topic distribution
    :param W:  the DxT document-term matrix, a sparse CSR matrix.
    :param docLens:  the length of each document
    :param topicMeans: the DxK matrix, where the d-th row contains the mean of all
                        per-token topic-assignments.
    :param topicPrior: the prior over topics
    :param diWordDists: the KxT matrix of word distributions, after the digamma funciton
                        has been applied
    :param diWordDistSums: the K-vector of the digamma of the sums of the Dirichlet
                            parameter vectors for each per-topic word-distribution
    :return: the indices of the non-zero words in document d, and the KxV matrix of
            topic assignments for each of the V non-zero words.
    '''
    K = diWordDists.shape[0]
    wordIdx, z = _infer_topics_at_d(d, W, docLens, topicMeans, topicPrior, diWordDists, diWordDistSums)
    topicMeans[d, :K] = np.dot(z, W[d, :].data) / docLens[d]
    return wordIdx, z

#@nb.jit
def _infer_topics_at_d(d, W, docLens, topicMeans, topicPrior, diWordDists, diWordDistSums):
    '''
    Infers the topic assignments for all present words in the given document at
    index d as stored in the sparse CSR matrix W. This does not affect topicMeans.
    The indices of the non-zero words, and their probabilities, are returned.

    :param d:  the document for which we update the topic distribution
    :param W:  the DxT document-term matrix, a sparse CSR matrix.
    :param docLens:  the length of each document
    :param topicMeans: the DxK matrix, where the d-th row contains the mean of all
                        per-token topic-assignments.
    :param topicPrior: the prior over topics
    :param diWordDists: the KxT matrix of word distributions, after the digamma funciton
                        has been applied
    :param diWordDistSums: the K-vector of the digamma of the sums of the Dirichlet
                            parameter vectors for each per-topic word-distribution
    :return: the indices of the non-zero words in document d, and the KxV matrix of
            topic assignments for each of the V non-zero words.
    '''
    K = diWordDists.shape[0]

    wordIdx = W[d, :].indices
    z  = diWordDists[:, wordIdx]
    z -= diWordDistSums[:, np.newaxis]

    distAtD = (topicPrior + docLens[d] * topicMeans[d, :])[:K, np.newaxis]

    z += fns.digamma(distAtD)
    z -= fns.digamma(distAtD.sum())

    _inplace_softmax_colwise(z)
    return wordIdx, z


#@nb.jit
def train(data, model, query, plan, updateVocab=True):
    '''
    Infers the topic distributions in general, and specifically for
    each individual datapoint, and additionally learns the weights
    needed to predict new links.

    Params:
    W - the DxT document-term matrix
    X - The DxD document-document matrix
    model - the initial model configuration. This is MUTATED IN-PLACE
    qyery - the query results - essentially all the "local" variables
            matched to the given observations. Also MUTATED IN-PLACE
    plan  - how to execute the training process (e.g. iterations,
            log-interval etc.)

    Return:
    The updated model object (note parameters are updated in place, so make a
    defensive copy if you want it)
    The query object with the update query parameters
    '''
    iterations, epsilon, logFrequency, fastButInaccurate, debug = \
        plan.iterations, plan.epsilon, plan.logFrequency, plan.fastButInaccurate, plan.debug
    docLens, topics, U, V, tsums_bydoc, tsums_bytop, exp_tsums_bydoc, exp_tsums_bytop, out_counts, in_counts = \
        query.docLens, query.topics, query.U, query.V, query.tsums_bydoc, query.tsums_bytop, query.exp_tsums_bydoc, query.exp_tsums_bytop, query.out_counts, query.in_counts
    K, Q, topicPrior, vocabPrior, wordDists, topicCov, dtype, name = \
	    model.K, model.Q, model.topicPrior, model.vocabPrior, model.wordDists, model.topicCov, model.dtype, model.name

    # Quick sanity check
    if np.any(docLens < 1):
        raise ValueError ("Input document-term matrix contains at least one document with no words")
    assert dtype == np.float64, "Only implemented for 64-bit floats"

    # Prepare the data for inference
    W   = data.words
    D,T = W.shape
    X   = data.links

    iters, bnds, likes = [], [], []

    # Instead of storing the full topic assignments for every individual word, we
    # re-estimate from scratch. I.e for the memberships z which is DxNxT in dimension,
    # we only store a 1xNxT = NxT part.
    z = np.empty((K,), dtype=dtype, order='F')
    diWordDistSums = np.empty((K,), dtype=dtype)
    diWordDists = np.empty(wordDists.shape, dtype=dtype)

    for itr in range(iterations):
        printAndFlushNoNewLine("\n %4d: " % itr)

        diWordDistSums[:] = wordDists.sum(axis=1)
        fns.digamma(diWordDistSums, out=diWordDistSums)
        fns.digamma(wordDists,      out=diWordDists)

        if updateVocab:
            wordDists[:, :] = vocabPrior
            for d in range(D):
                if d % 100 == 0:
                    printAndFlushNoNewLine(".")
                # wordIdx, z = _update_topics_at_d(d, W, docLens, topicMeans, topicPrior, diWordDists, diWordDistSums)
                # wordDists[:, wordIdx] += W[d, :].data[np.newaxis, :] * z

            if True: # itr % logFrequency == 0:
                iters.append(itr)
                bnds.append(_var_bound_internal(data, model, query))
                likes.append(_log_likelihood_internal(data, model, query))

                if converged(iters, bnds, len(bnds) - 1, minIters=5):
                    break

        else:
            for d in range(D):
                pass
                # wordIdx, z = _update_topics_at_d(d, W, docLens, topicMeans, topicPrior, diWordDists, diWordDistSums)


    return ModelState(K, Q, topicPrior, vocabPrior, wordDists, dtype, model.name), \
           QueryState(docLens, topics, U, V, tsums_bydoc, tsums_bytop, exp_tsums_bydoc, exp_tsums_bytop, out_counts, in_counts), \
           (np.array(iters, dtype=np.int32), np.array(bnds), np.array(likes))


def printAndFlushNoNewLine (text):
    sys.stdout.write(text)
    sys.stdout.flush()

#@nb.jit
def query(data, model, query, plan):
    '''
    Infers the topic distributions in general, and specifically for
    each individual datapoint, without altering the model

    Params:
    W - the DxT document-term matrix
    X - The DxD document-document matrix
    model - the initial model configuration. This is MUTATED IN-PLACE
    qyery - the query results - essentially all the "local" variables
            matched to the given observations. Also MUTATED IN-PLACE
    plan  - how to execute the training process (e.g. iterations,
            log-interval etc.)

    Return:
    The updated model object (note parameters are updated in place, so make a
    defensive copy if you want it)
    The query object with the update query parameters
    '''
    _, topics, (_,_,_) =  train(data, model, query, plan, updateVocab=False)
    return model, topics

#@nb.jit
def _var_bound_internal(data, model, query, z_dnk = None):
    result = var_bound(data, model, query, z_dnk)

    return result


#@nb.jit
def var_bound(data, model, query, z_dnk = None):
    '''
    Determines the variational bounds.
    '''
    bound = 0
    
    # Unpack the the structs, for ease of access and efficiency
    docLens, topics, U, V, tsums_bydoc, tsums_bytop, exp_tsums_bydoc, exp_tsums_bytop, out_counts, in_counts = \
        query.docLens, query.topics, query.U, query.V, query.tsums_bydoc, query.tsums_bytop, query.exp_tsums_bydoc, query.exp_tsums_bytop, query.out_counts, query.in_counts
    K, Q, topicPrior, vocabPrior, wordDists, topicCov, dtype, name = \
	    model.K, model.Q, model.topicPrior, model.vocabPrior, model.wordDists, model.topicCov, model.dtype, model.name

    W, X = data.words, data.links
    D, T = W.shape
    bound = 0

    # ln p(U)
    bound += -log(2*pi) - D * Q * log(Vagueness) - 0.5 *  1./Vagueness * 1./Vagueness * np.sum(U * U) # trace of U U'

    # H[q(U)]
    bound += -(D * Q * 0.5) * log(2 * pi * e) - D * Q * log(Vagueness)

    # ln p(V)
    bound += -log(2*pi) - D * Q * log(Vagueness) - 0.5 *  1./Vagueness * 1./Vagueness * np.sum(V * V) # trace of U U'

    # H[q(V)]
    bound += -(D * Q * 0.5) * log(2 * pi * e) - D * Q * log(Vagueness)

    # ln p(Topics|U, V)
    logDetCov = log (la.det(topicCov))
    kernel = topics.copy()
    kernel -= U.T.dot(V)
    kernel **= 2
    bound -= -log(2*pi) - D * K * 0.5 * log (Vagueness) \
             -D * 0.5 * logDetCov \
             - 0.5 * np.sum(kernel)




    return 0


def _dirichletEntropy (P):
    '''
    Entropy of D Dirichlet distributions, with dimension K, whose parameters
    are given by the DxK matrix P
    '''
    D,K    = P.shape
    psums  = P.sum(axis=1)
    lnB   = fns.gammaln(P).sum(axis=1) - fns.gammaln(psums)
    term1 = (psums - K) * fns.digamma(psums)
    term2 = (P - 1) * fns.digamma(P)
    
    return (lnB + term1 - term2.sum(axis=1)).sum()



@nb.jit
def min_link_probs(model, topics, links):
    '''
    For every document, for each of the given links, determine the
    probability of the least likely link (i.e the document-specific
    minimum of probabilities).

    :param model: the model object
    :param topics: the topics that were inferred for each document
        represented by the links matrix
    :param links: a DxD matrix of links for each document (row)
    :return: a D-dimensional vector with the minimum probabilties for each
        link
    '''
    weights    = model.weights
    topicMeans = topics.topicDists
    D = topicMeans.shape[0]

    # derive topic means from the topic distributions
    # note there is a risk of loss of precision in all this which I just accept
    topicPriorExt = extend_topic_prior(model.topicPrior, 0)
    topicMeans -= topicPriorExt[np.newaxis,:]
    topicMeans /= topics.docLens[:, np.newaxis]

    # use the topics means to predict links
    mins = np.ones((D,), dtype=model.dtype)
    for d in range(D):
        probs = (topicMeans[d] * topicMeans[links[d,:].indices]).dot(weights)
        mins[d] = compiled.probit(probs).min()

    # return from topic means to the topic distributions
    topicMeans *= topics.docLens[:, np.newaxis]
    topicMeans += topicPriorExt[np.newaxis,:]

    return mins

def extend_topic_prior (prior_vec, extra_field):
    return np.hstack ((prior_vec, extra_field))

def link_probs(model, topics, min_link_probs):
    '''
    Generate the probability of a link for all possible pairs of documents,
    but only store those probabilities that are bigger than or equal to the
    minimum. This ensures, hopefully, that we don't materialise a complete
    DxD matrix, but rather the minimum needed to determine the mean
    average precsions

    :param model: the trained model
    :param topics: the topics for each of teh documents we're generating
        links for
    :param min_link_probs: the minimum link probability for each document
    :return: a (hopefully) sparse DxD matrix of link probabilities
    '''
    weights    = model.weights
    topicMeans = topics.topicDists
    D = topicMeans.shape[0]

    # We build the result up as a COO matrix
    rows = []
    cols = []
    vals = []

    # derive topic means from the topic distributions
    # note there is a risk of loss of precision in all this which I just accept
    topicPriorExt = extend_topic_prior(model.topicPrior, 0)
    topicMeans -= topicPriorExt[np.newaxis,:]
    topicMeans /= topics.docLens[:, np.newaxis]

    # use the topics means to predict links
    mins = np.ones((D,), dtype=model.dtype)
    for d in range(D):
        probs = (topicMeans[d] * topicMeans).dot(weights)
        probs = compiled.probit(probs)
        relevant = np.where(probs >= mins[d])[0]
        print ("Non-neglible links: %d / %d" % (len(relevant), D))

        rows.extend[[d] * len(relevant)]
        cols.extend(relevant)
        vals.extend(probs[relevant])

    # return from topic means to the topic distributions
    topicMeans *= topics.docLens[:, np.newaxis]
    topicMeans += topicPriorExt[np.newaxis,:]

    # Build the COO matrix, then covert it to CSR. Converts lists to numpy
    # arrays to ensure appropriate dtypes
    r = np.array(rows, dtype=np.int32)
    c = np.array(cols, dtype=np.int32)
    v = np.array(vals, dtype=model.dtype)

    return ssp.coo_matrix((v (r, c)), shape=(D,D)).tocsr()


if __name__ == '__main__':
    test = np.array([-1, 3, 5, -4 , 4, -3, 1], dtype=np.float64)
    print (str (compiled.normpdf(test)))
