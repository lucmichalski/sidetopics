'''
Contains a number of functions required to implement LDA/VB
in a fast manner. The implementation also relies on the word list 
functions in lda_cvb_fast.pyx.

As is typically the case, there are multiple implementations for multiple
different datatypes. 

Compilation Notes
=================
This code expects a compiler with OpenMP support, and will multi-thread
certain operations where it offers benefit. On GCC this means you must 
link to the "gomp" library. You will also need to link to the standard C
math library and the Gnu scientific library (libgsl)

To Dos
=================
TODO for the fastButInaccurate options, consider multithreading across
the D documents, ignoring the serial dependence on z_dnk, and then
recalculate the counts from z_dnk afterwards.
'''

cimport cython
import numpy as np
cimport numpy as np

import scipy.special as fns

from cython.parallel cimport parallel, prange
from libc.stdlib cimport rand, srand, malloc, free, RAND_MAX
from libc.math cimport log, exp, sqrt, fabs, isnan, isinf
from libc.float cimport DBL_MAX, DBL_MIN, FLT_MAX, FLT_MIN
#from openmp cimport omp_set_num_threads

cdef int MaxInnerItrs = 100
cdef int MinInnerIters = 3

cdef extern from "gsl/gsl_sf_result.h":
    ctypedef struct gsl_sf_result:
        double val
        double err
cdef extern from "gsl/gsl_sf_psi.h":
    double gsl_sf_psi(double x) nogil
    double gsl_sf_psi_1(double x) nogil
    
cdef double digamma (double value) nogil:
    if value < 1E-300:
        value = 1E-300
    return gsl_sf_psi (value)

cdef double trigamma (double value) nogil:
    if value < 1E-300:
        value = 1E-300
    return gsl_sf_psi_1 (value)
  

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
def iterate_f32(int iterations, int D, int K, int T, \
                 int[:,:] W_list, int[:] docLens, \
                 float[:] topicPrior, float vocabPrior, \
                 float[:,:] z_dnk, float[:,:] topicDists, float[:,:] vocabDists):
    '''
    Performs the given number of iterations as part of the training
    procedure. There are two corpora, the model corpus of files, whose
    topic assignments are fixed and who constitute the prior (in this case
    the prior represented by real instead of pseudo-counts) and the query
    corpus for which we estimate topic assignments. We have the query-specific
    matrix of tokens generated by topic k for corpus d, then we have corpus
    wide matrices (though still prefixed by "q_") of word-counts per topic
    (the vocabularly distributions) and topic counts over all (the topic
    prior)
    
    Params:
    iterations - the number of iterations to perform
    D          - the number of documents
    K          - the number of topics
    T          - the number of possible words
    W_list     - a jagged DxN_d array of word-observations for each document
    docLens    - the length of each document d
    topicPrior - the K dimensional vector with the topic prior
    vocabPrior - the T dimensional vector with the vocabulary prior
    z_dnk      - a max_d(N_d) x K dimensional matrix, containing all possible topic
                 assignments for a single document
    topicDists - the D x K matrix of per-document, topic probabilities. This _must_
                 be in C (i.e row-major) format.
    vocabDists - the K x T matrix of per-topic word probabilties
    '''
    
    cdef:
        int         d, n, k, t
        int         itr, totalItrs
        float[:,:] oldVocabDists = np.ndarray(shape=(K,T), dtype=np.float32)
        float[:,:] newVocabDists = vocabDists
        float[:]   oldMems       = np.ndarray(shape=(K,), dtype=np.float32)
        float[:]   vocabNorm     = np.ndarray(shape=(K,), dtype=np.float32)
        
        float      z # These four for the hyperprior update
        float[:]   q = np.ndarray(shape=(K,), dtype=np.float32)
        float[:]   g = np.ndarray(shape=(K,), dtype=np.float32)
        float      b
        float      topicPriorSum
        
    totalItrs = 0
    for itr in range(iterations):
        oldVocabDists, newVocabDists = newVocabDists, oldVocabDists
        
        vocabNorm[:]       = vocabPrior * T
        newVocabDists[:,:] = vocabPrior
        
        topicPriorSum = np.sum(topicPrior)
        
        with nogil:
            for d in range(D):
                # Figure out document d's topic distribution, this is
                # written into topicDists and z_dnk
                totalItrs += infer_topics_f32(d, K, \
                                              W_list, docLens, \
                                              topicPrior, topicPriorSum, \
                                              z_dnk, oldMems, topicDists, \
                                              oldVocabDists)
                
                # Then use those to gradually update our new vocabulary
                for k in range(K):
                    for n in range(docLens[d]):
                        t = W_list[d,n]
                        newVocabDists[k,t] += z_dnk[n,k]
                        vocabNorm[k] += z_dnk[n,k]
                        
                        if is_invalid(newVocabDists[k,t]):
                            with gil:
                                print ("newVocabDist[%d,%d] = %f, z_dnk[%d,%d] = %f" \
                                      % (k, t, newVocabDists[k,t], n, k, z_dnk[n,k]))
                            
            # With all documents processed, normalize the vocabulary
            for k in prange(K):
                for t in range(T):
                    newVocabDists[k,t] /= vocabNorm[k]
                    
        # And update the prior on the topic distribution. We
        # do this with the GIL, as built-in numpy is likely faster
#        for _ in range(20):
#            z = D * fns.polygamma(1, topicPriorSum)
#            q = -D * fns.polygamma(1, topicPrior)
#            
#            g = fns.psi(topicPrior) * -D 
#            g += D * fns.psi(topicPriorSum)
#            g += np.sum(fns.psi(topicDists) - fns.psi(np.sum(topicDists, axis=1)), axis=0)
#            g -= np.sum ()   
#            
#            b = np.sum(np.divide(g, q))
#            b /= (1./z + np.sum(np.reciprocal(q)))
#            topicPrior -= (np.divide(np.subtract (g, b), q))
                
    # Just before we return, make sure the vocabDists memoryview that
    # was passed in has the latest vocabulary distributions
    if iterations % 2 == 0:
        vocabDists[:,:] = newVocabDists
            
    print ("Average inner iterations %f" % (float(totalItrs) / (D*iterations)))
    
#    topicPriorStr = str(topicPrior[0])
#    for k in range(1,K):
#        topicPriorStr += ", " + str(topicPrior[k])
#    print ("Topic prior is " + topicPriorStr)
    return totalItrs                        

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
def query_f32(int D, int K, \
                 int[:,:] W_list, int[:] docLens, \
                 float[:] topicPrior, float[:,:] z_dnk, float[:,:] topicDists, 
                 float[:,:] vocabDists):
    cdef:
        int        d
        float[:]   oldMems       = np.ndarray(shape=(K,), dtype=np.float32)
        float      topicPriorSum = np.sum(topicPrior)
    
    with nogil:
        for d in range(D):
            infer_topics_f32(d, K, \
                 W_list, docLens, \
                 topicPrior, topicPriorSum,
                 z_dnk, 
                 oldMems, topicDists, 
                 vocabDists)

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
cdef inline int infer_topics_f32(int d, int K, \
                 int[:,:] W_list, int[:] docLens, \
                 float[:] topicPrior, float topicPriorSum,
                 float[:,:] z_dnk, 
                 float[:] oldMems, float[:,:] topicDists, 
                 float[:,:] vocabDists) nogil:
    '''             
    Infers the topic assignments for a given document d with a fixed vocab and
    topic prior. The topicDists and z_dnk are mutated in-place. Everything else
    remains constant.
    
    Params:
    d          - the document to infer topics for.
    K          - the number of topics
    W_list     - a jagged DxN_d array of word-observations for each document
    docLens    - the length of each document d
    topicPrior - the K dimensional vector with the topic prior
    z_dnk      - a max_d(N_d) x K dimensional matrix, containing all possible topic
                 assignments for a single document
    oldMems    - a previously allocated K-dimensional vector to hold the previous
                 topic assignments for document d
    topicDists - the D x K matrix of per-document, topic probabilities. This _must_
                 be in C (i.e row-major) format.
    vocabDists - the K x T matrix of per-topic word probabilties
    '''
    cdef:
        int        k
        int        n
        int        innerItrs
        float      max  = 1E-311
        float      norm = 0.0
        float      epsilon = 0.01 / K
    
    
    # NOTE THIS CODE COPY AND PASTED INTO lda_vb.var_bound() !
    
    # For each document reset the topic probabilities and iterate to
    # convergence. This means we don't have to store the per-token
    # topic probabilties z_dnk for all documents, which is a huge saving
    oldMems[:]      = topicDists[d,:]
    topicDists[d,:] = 1./K
#    initAtRandom_f32(topicDists, d, K)
    innerItrs = 0
    
    while ((innerItrs < MinInnerIters) or (l1_dist_f32 (oldMems, topicDists[d,:]) > epsilon)) \
    and (innerItrs < MaxInnerItrs):
        oldMems[:] = topicDists[d,:]
        innerItrs += 1
        
        # Determine the topic assignment for each individual token...
        for n in range(docLens[d]):
            norm = 0.0
            max  = 1E-311
            
            # Work in log-space to avoid underflow
            for k in range(K):
                z_dnk[n,k] = log(vocabDists[k,W_list[d,n]]) + digamma(topicDists[d,k])
                if z_dnk[n,k] > max:
                    max = z_dnk[n,k]

            # Scale before converting to standard space so inference is feasible
            for k in range(K):
                z_dnk[n,k] = exp(z_dnk[n,k] - max)
                norm += z_dnk[n,k]
         
            # Normalize the token probability, and check it's valid
            for k in range(K):
                z_dnk[n,k] /= norm
                if is_invalid(z_dnk[n,k]):
                    with gil:
                        print ("Inner iteration %d z_dnk[%d,%d] = %f, norm = %g" \
                               % (innerItrs, n, k, z_dnk[n,k], norm))

        # Use all the individual word topic assignments to determine
        # the topic mixture exhibited by this document
        topicDists[d,:] = topicPrior
        norm = topicPriorSum
        for n in range(docLens[d]):
            for k in range(K):
                topicDists[d,k] += z_dnk[n,k]
                norm += z_dnk[n,k]
                
        for k in range(K):
            topicDists[d,k] /= norm   
                    
    return innerItrs

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
cdef inline void initAtRandom_f32(float[:,:] topicDists, int d, int K) nogil:
    cdef:
        float norm = 0.0
    
    for k in range(K):
        topicDists[d,k] = rand()
        norm += topicDists[d,k]
    
    for k in range(K):
        topicDists[d,k] /= norm                     
                        

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
cdef float l1_dist_f32 (float[:] left, float[:] right) nogil:
    cdef:
        int i = 0
        float result = 0.0
        
    for i in range(left.shape[0]):
        result += fabs(left[i] - right[i])
    
    return result 


cdef bint is_invalid (double zdnk) nogil:
    return isnan(zdnk) \
        or isinf(zdnk) \
        or zdnk < -0.001
#        or zdnk > 1.001



@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
def iterate_f64(int iterations, int D, int K, int T, \
                 int[:,:] W_list, int[:] docLens, \
                 double[:] topicPrior, double vocabPrior, \
                 double[:,:] z_dnk, double[:,:] topicDists, double[:,:] vocabDists):
    '''
    Performs the given number of iterations as part of the training
    procedure. There are two corpora, the model corpus of files, whose
    topic assignments are fixed and who constitute the prior (in this case
    the prior represented by real instead of pseudo-counts) and the query
    corpus for which we estimate topic assignments. We have the query-specific
    matrix of tokens generated by topic k for corpus d, then we have corpus
    wide matrices (though still prefixed by "q_") of word-counts per topic
    (the vocabularly distributions) and topic counts over all (the topic
    prior)
    
    Params:
    iterations - the number of iterations to perform
    D          - the number of documents
    K          - the number of topics
    T          - the number of possible words
    W_list     - a jagged DxN_d array of word-observations for each document
    docLens    - the length of each document d
    topicPrior - the K dimensional vector with the topic prior
    vocabPrior - the T dimensional vector with the vocabulary prior
    z_dnk      - a max_d(N_d) x K dimensional matrix, containing all possible topic
                 assignments for a single document
    topicDists - the D x K matrix of per-document, topic probabilities. This _must_
                 be in C (i.e row-major) format.
    vocabDists - the K x T matrix of per-topic word probabilties
    '''
    
    cdef:
        int         d, n, k, t
        int         itr, totalItrs
        double[:,:] oldVocabDists = np.ndarray(shape=(K,T), dtype=np.float64)
        double[:,:] newVocabDists = vocabDists
        double[:]   oldMems       = np.ndarray(shape=(K,), dtype=np.float64)
        double[:]   vocabNorm     = np.ndarray(shape=(K,), dtype=np.float64)
        
        double      z # These four for the hyperprior update
        double[:]   q = np.ndarray(shape=(K,), dtype=np.float64)
        double[:]   g = np.ndarray(shape=(K,), dtype=np.float64)
        double      b
        double      topicPriorSum
        
    totalItrs = 0
    for itr in range(iterations):
        oldVocabDists, newVocabDists = newVocabDists, oldVocabDists
        
        vocabNorm[:]       = vocabPrior * T
        newVocabDists[:,:] = vocabPrior
        
        topicPriorSum = np.sum(topicPrior)
        
        with nogil:
            for d in range(D):
                # Figure out document d's topic distribution, this is
                # written into topicDists and z_dnk
                totalItrs += infer_topics_f64(d, D, K, \
                                              W_list, docLens, \
                                              topicPrior, topicPriorSum, \
                                              z_dnk, oldMems, topicDists, \
                                              oldVocabDists)
                
                # Then use those to gradually update our new vocabulary
                for k in range(K):
                    for n in range(docLens[d]):
                        t = W_list[d,n]
                        newVocabDists[k,t] += z_dnk[n,k]
                        vocabNorm[k] += z_dnk[n,k]
                        
                        if is_invalid(newVocabDists[k,t]):
                            with gil:
                                print ("newVocabDist[%d,%d] = %f, z_dnk[%d,%d] = %f" \
                                      % (k, t, newVocabDists[k,t], n, k, z_dnk[n,k]))
                            
            # With all documents processed, normalize the vocabulary
            for k in prange(K):
                for t in range(T):
                    newVocabDists[k,t] /= vocabNorm[k]
                    
        # And update the prior on the topic distribution. We
        # do this with the GIL, as built-in numpy is likely faster
#        for _ in range(20):
#            z = D * fns.polygamma(1, topicPriorSum)
#            q = -D * fns.polygamma(1, topicPrior)
#            
#            g = fns.psi(topicPrior) * -D 
#            g += D * fns.psi(topicPriorSum)
#            g += np.sum(fns.psi(topicDists) - fns.psi(np.sum(topicDists, axis=1)), axis=0)
#            g -= np.sum ()   
#            
#            b = np.sum(np.divide(g, q))
#            b /= (1./z + np.sum(np.reciprocal(q)))
#            topicPrior -= (np.divide(np.subtract (g, b), q))
                
    # Just before we return, make sure the vocabDists memoryview that
    # was passed in has the latest vocabulary distributions
    if iterations % 2 == 0:
        vocabDists[:,:] = newVocabDists
            
    print ("Average inner iterations %f" % (float(totalItrs) / (D*iterations)))
    
#    topicPriorStr = str(topicPrior[0])
#    for k in range(1,K):
#        topicPriorStr += ", " + str(topicPrior[k])
#    print ("Topic prior is " + topicPriorStr)
    return totalItrs                        

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
def query_f64(int D, int K, \
                 int[:,:] W_list, int[:] docLens, \
                 double[:] topicPrior, double[:,:] z_dnk, double[:,:] topicDists, 
                 double[:,:] vocabDists):
    cdef:
        int         d
        double[:]   oldMems       = np.ndarray(shape=(K,), dtype=np.float64)
        double      topicPriorSum = np.sum(topicPrior)
    
    with nogil:
        for d in range(D):
            infer_topics_f64(d, D, K, \
                 W_list, docLens, \
                 topicPrior, topicPriorSum,
                 z_dnk, 
                 oldMems, topicDists, 
                 vocabDists)

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
cdef inline int infer_topics_f64(int d, int D, int K, \
                 int[:,:] W_list, int[:] docLens, \
                 double[:] topicPrior, double topicPriorSum,
                 double[:,:] z_dnk, 
                 double[:] oldMems, double[:,:] topicDists, 
                 double[:,:] vocabDists) nogil:
    '''             
    Infers the topic assignments for a given document d with a fixed vocab and
    topic prior. The topicDists and z_dnk are mutated in-place. Everything else
    remains constant.
    
    Params:
    d          - the document to infer topics for.
    K          - the number of topics
    W_list     - a jagged DxN_d array of word-observations for each document
    docLens    - the length of each document d
    topicPrior - the K dimensional vector with the topic prior
    z_dnk      - a max_d(N_d) x K dimensional matrix, containing all possible topic
                 assignments for a single document
    oldMems    - a previously allocated K-dimensional vector to hold the previous
                 topic assignments for document d
    topicDists - the D x K matrix of per-document, topic probabilities. This _must_
                 be in C (i.e row-major) format.
    vocabDists - the K x T matrix of per-topic word probabilties
    '''
    cdef:
        int         k
        int         n
        int         innerItrs
        double      max  = 1E-311
        double      norm = 0.0
        double      epsilon = 0.01 / K
        double      post
    
    
    # NOTE THIS CODE COPY AND PASTED INTO lda_vb.var_bound() !
    
    # For each document reset the topic probabilities and iterate to
    # convergence. This means we don't have to store the per-token
    # topic probabilties z_dnk for all documents, which is a huge saving
    oldMems[:]      = 1./K
    topicDists[d,:] = topicPrior
    
    post = D
    post /= K
    for k in range(K):
        topicDists[d,k] += post
#    initAtRandom_f64(topicDists, d, K)
    innerItrs = 0
    
    while ((innerItrs < MinInnerIters) or (l1_dist_f64 (oldMems, topicDists[d,:]) > epsilon)) \
    and (innerItrs < MaxInnerItrs):
        oldMems[:] = topicDists[d,:]
        innerItrs += 1
        
        # Determine the topic assignment for each individual token...
        for n in range(docLens[d]):
            norm = 0.0
            max  = 1E-311
            
            # Work in log-space to avoid underflow
            for k in range(K):
                z_dnk[n,k] = log(vocabDists[k,W_list[d,n]]) + digamma(topicDists[d,k])
                if z_dnk[n,k] > max:
                    max = z_dnk[n,k]

            # Scale before converting to standard space so inference is feasible
            for k in range(K):
                z_dnk[n,k] = exp(z_dnk[n,k] - max)
                norm += z_dnk[n,k]
         
            # Normalize the token probability, and check it's valid
            for k in range(K):
                z_dnk[n,k] /= norm
                if is_invalid(z_dnk[n,k]):
                    with gil:
                        print ("Inner iteration %d z_dnk[%d,%d] = %f, norm = %g" \
                               % (innerItrs, n, k, z_dnk[n,k], norm))

        # Use all the individual word topic assignments to determine
        # the topic mixture exhibited by this document
        topicDists[d,:] = topicPrior
        norm = topicPriorSum
        for n in range(docLens[d]):
            for k in range(K):
                topicDists[d,k] += z_dnk[n,k]
                norm += z_dnk[n,k]
                
        for k in range(K):
            topicDists[d,k] /= norm   
                    
    return innerItrs

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
cdef inline void initAtRandom_f64(double[:,:] topicDists, int d, int K) nogil:
    cdef:
        double norm = 0.0
    
    for k in range(K):
        topicDists[d,k] = rand()
        norm += topicDists[d,k]
    
    for k in range(K):
        topicDists[d,k] /= norm

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
cdef double l1_dist_f64 (double[:] left, double[:] right) nogil:
    cdef:
        int i = 0
        double result = 0.0
        
    for i in range(left.shape[0]):
        result += fabs(left[i] - right[i])
    
    return result 



