#!/usr/bin/python
# -*- coding: utf-8 -*- 

'''
Created on 15 Oct 2013

@author: bryanfeeney
'''
from __future__ import division
import unittest


from model.sidetopic_uyv import newVbModelState, train, query, rowwise_softmax, log_likelihood
from model_test.sidetopic_test import makeSixTopicVocab, matrix_normal
from util.overflow_safe import safe_log

import numpy as np
import scipy.linalg as la
import numpy.random as rd
import scipy.sparse as ssp
from math import ceil


def sampleFromModel(D=200, T=100, K=10, Q=6, F=12, P=8, avgWordsPerDoc = 500):
    '''
    Create a test dataset according to the model
    
    Params:
        T - Vocabulary size, the number of "terms". Must be a square number
        Q - Latent Topics:
        K - Observed topics
        P - Latent features
        F - Observed features
        D - Sample documents (each with associated features)
        avgWordsPerDoc - average number of words per document generated (Poisson)
    
    Returns:
        modelState - a model state object configured for training
        tpcs       - the matrix of per-document topic distribution
        vocab      - the matrix of per-topic word distributions
        docLens    - the vector of document lengths
        X          - the DxF side information matrix
        W          - The DxW word matrix
    '''
    
    # Generate vocab
    beta = 0.1
    betaVec = np.ndarray((T,))
    betaVec.fill(beta)
    vocab = np.zeros((K,T))
    for k in range(K):
        vocab[k,:] = rd.dirichlet(betaVec)
    
    # Generate U, then V, then A
    tau = 0.1
    tsq = tau * tau
    (vSdRow, vSdCol) = (5.0, 5.0)
    (uSdRow, uSdCol) = (5.0, tau**2) # For the K-dimensions we use tsq
    (ySdRow, ySdCol) = (5.0, 5.0)
    (aSdRow, aSdCol) = (5.0, tau**2)
    
    U = np.abs(matrix_normal(np.zeros((K,Q)),   uSdRow * np.eye(Q), uSdCol * np.eye(K)))
    Y = np.abs(matrix_normal(np.zeros((Q,P)),   ySdRow * np.eye(P), ySdCol * np.eye(Q)))
    V = np.abs(matrix_normal(np.zeros((F,P)),   vSdRow * np.eye(P), vSdCol * np.eye(F)))
    A = np.abs(matrix_normal(U.dot(Y).dot(V.T), aSdRow * np.eye(F), aSdCol * np.eye(K)))
    
    # Generate the input features. Assume the features are multinomial and sparse
    # (not quite a perfect match for the twitter example: twitter is binary, this 
    # may not be)
    featuresDist  = [1. / P] * P
    maxNonZeroFeatures = 3
    
    X_low = np.zeros((D,P), dtype=np.float32)
    for d in range(D):
        X_low[d,:] = rd.multinomial(maxNonZeroFeatures, featuresDist)
    X = np.round(X_low.dot(V.T))
    X = ssp.csr_matrix(X)
    
    # Use the features and the matrix A to generate the topics and documents
    tpcs = rowwise_softmax (X.dot(A.T))
    
    docLens = rd.poisson(avgWordsPerDoc, (D,)).astype(np.float32)
    W = tpcs.dot(vocab)
    W *= docLens[:, np.newaxis]
    W = np.array(W, dtype=np.int32) # truncate word counts to integers
    W = ssp.csr_matrix(W)
    
    # Initialise the model
    modelState = newVbModelState(K, Q, F, P, T)
    
    # Return the initialised model, the true parameter values, and the
    # generated observations
    return modelState, tpcs, vocab, docLens, X, W

class StUyvTest(unittest.TestCase):
    '''
    Provides basic unit tests for the variational SideTopic inference engine using
    A=UYV with small inputs derived from known parameters.
    '''

    def setUp(self):
        pass


    def tearDown(self):
        pass

        
    def testLikelihoodOnModelDerivedExample(self):
        print("Cross-validated likelihoods on model-derived example")
        
        rd.seed(0xBADB055) # Global init for repeatable test
        modelState, _, _, _, X, W = sampleFromModel()
        D, T, K, Q, F, P = X.shape[0], modelState.T, modelState.K, modelState.Q, modelState.F, modelState.P
        
        # Create the cross-validation folds
        folds     = 5
        foldSize  = ceil(D / 5)
        querySize = foldSize
        trainSize = D - querySize
        
        for fold in range(folds):
            start = fold * foldSize
            end   = start + trainSize
            
            trainSet = np.arange(start,end) % D
            querySet = np.arange(end, end + querySize) % D
            
            X_train, W_train = X[trainSet,:], W[trainSet,:]
            X_query, W_query = X[querySet,:], W[querySet,:]
            
            modelState = newVbModelState(K, Q, F, P, T)
            modelState, queryState = train(modelState, X_train, W_train, iterations=100, logInterval=10, plotInterval=100)
            trainSetLikely = log_likelihood(modelState, X_train, W_train, queryState)
            
            queryState = query(modelState, X_query, W_query, iterations=50, epsilon=0.001, logInterval = 10, plotInterval = 100)
            querySetLikely = log_likelihood(modelState, X_query, W_query, queryState)
            
            print("Fold %d: Train-set Likelihood: %12f \t Query-set Likelihood: %12f" % (fold, trainSetLikely, querySetLikely))
           
        print("End of Test")
    
        
    def _testInferenceOnModelDerivedData(self):
        print("Model derived example")
        
        rd.seed(0xBADB055) # Global init for repeatable test
        modelState, tpcs, _, _, X, W = self._sampleFromModel()
        D = X.shape[0]
        
        (trainedState, queryState) = train (modelState, X, W, logInterval=1, iterations=1)
        tpcs_inf = rowwise_softmax(np.log(queryState.expLmda)) # why safe?
        W_inf    = np.array(tpcs_inf.dot(trainedState.vocab) * queryState.docLen[:,np.newaxis], dtype=np.int32)
        priorReconsError = np.sum(np.square(W - W_inf)) / D
        
        (trainedState, queryState) = train (modelState, X, W, logInterval=1, plotInterval = 50, iterations=200)
        tpcs_inf = rowwise_softmax(np.log(queryState.expLmda))
        W_inf    = np.array(tpcs_inf.dot(trainedState.vocab) * queryState.docLen[:,np.newaxis], dtype=np.int32)
        
        print ("Model Driven: Prior Reconstruction Error: %f" % (priorReconsError,))
        print ("Model Driven: Final Reconstruction Error: %f" % (np.sum(np.square(W - W_inf)) / D,))
        
        print (str(tpcs[4,:]))
        print (str(tpcs_inf[4,:]))
        
        print("End of Test")       
        

    def _testInferenceFromHandcraftedExample(self):
        print ("Partially hand-crafted example")
        rd.seed(0xC0FFEE) # Global init for repeatable test
        
        T = 100 # Vocabulary size, the number of "terms". Must be a square number
        Q = 6   # Topics: This cannot be changed without changing the code that generates the vocabulary
        K = 10  # Observed topics
        P = 8   # Features
        F = 12  # Observed features
        D = 200 # Sample documents (each with associated features) 
        
        avgWordsPerDoc = 500
        
        # Determine what A, U, Y and V should be
        U = rd.random((K,Q))
        Y = rd.random((Q,P))
        V = rd.random((F,P))
        A = U.dot(Y).dot(V.T)
        
        # The vocabulary. Presented graphically there are two with horizontal bands
        # (upper lower); two with vertical bands (left, right);  and two with 
        # horizontal bands (inside, outside)
        vocab = makeSixTopicVocab(T)
        
        # Create our (sparse) features X, then our topic proportions ("tpcs")
        # then our word counts W
        X_low = np.array([1 if rd.random() < 0.3 else 0 for _ in range(D*P)]).reshape(D,P)
        X     = ssp.csr_matrix(X_low.dot(V.T))
        
        lmda_low = X_low.dot(Y.T)
        print ("lmda_low.mean() = %f" % (lmda_low.mean()))
        tpcs = rowwise_softmax (lmda_low)
        
        docLens = rd.poisson(avgWordsPerDoc, (D,))
        W = tpcs.dot(vocab)
        W *= docLens[:, np.newaxis]
        W = np.array(W, dtype=np.int32) # truncate word counts to integers
        W = ssp.csr_matrix(W)
        
        #
        # Now finally try to train the model
        #
        modelState = newVbModelState(K, Q, F, P, T)
        (trainedState, queryState) = train (modelState, X, W, logInterval=1, plotInterval = 10, iterations=10)
        
        tpcs_inf = rowwise_softmax(queryState.lmda)
        W_inf    = np.array(tpcs_inf.dot(trainedState.vocab) * queryState.docLen[:,np.newaxis], dtype=np.int32)
                
        print("Handcrafted Test-Case")
        print("=====================================================================")
        print("Average, squared, per-element difference between true and estimated:")
        print("    Topic Distribution:    %f" % (np.sum(np.square(tpcs.dot(U.T) - tpcs_inf)) / len(tpcs),))
        print("    Vocab Distribution:    %f" % (np.sum(np.square(U.dot(vocab) - trainedState.vocab)) / len(vocab),))
        print("Average absolute difference between true and reconstructed documents")
        print("    Documents:             %f" % (np.sum(np.abs(W.todense() - W_inf)) / np.sum(W.todense()),))
        
        
        print("End of Test")
        
        

    def _testInferenceFromHandcraftedExampleWithKEqualingQ(self):
        print ("Fully handcrafted example, K=Q")
        rd.seed(0xC0FFEE) # Global init for repeatable test
        
        T = 100 # Vocabulary size, the number of "terms". Must be a square number
        Q = 6   # Topics: This cannot be changed without changing the code that generates the vocabulary
        K = 6   # Observed topics
        P = 8   # Features
        F = 12  # Observed features
        D = 200 # Sample documents (each with associated features) 
        
        avgWordsPerDoc = 500
        
        # The vocabulary. Presented graphically there are two with horizontal bands
        # (upper lower); two with vertical bands (left, right);  and two with 
        # horizontal bands (inside, outside)
        vocab = makeSixTopicVocab(T)
        
        # Create our (sparse) features X, then our topic proportions ("tpcs")
        # then our word counts W
        lmda = np.zeros((D,K))
        X    = np.zeros((D,F))
        for d in range(D):
            for _ in range(3):
                lmda[d,rd.randint(K)] += 1./3
            for _ in range(int(F/3)):
                X[d,rd.randint(F)] += 1
        
        A = rd.random((K,F))
        X = lmda.dot(la.pinv(A).T)
        X = ssp.csr_matrix(X)
        
        tpcs = lmda
        
        docLens = rd.poisson(avgWordsPerDoc, (D,))
        W = tpcs.dot(vocab)
        W *= docLens[:, np.newaxis]
        W = np.array(W, dtype=np.int32) # truncate word counts to integers
        W = ssp.csr_matrix(W)
        
        #
        # Now finally try to train the model
        #
        modelState = newVbModelState(K, Q, F, P, T)
        
        (trainedState, queryState) = train (modelState, X, W, logInterval=1, iterations=1)
        tpcs_inf = rowwise_softmax(safe_log(queryState.expLmda))
        W_inf    = np.array(tpcs_inf.dot(trainedState.vocab) * queryState.docLen[:,np.newaxis], dtype=np.int32)
        priorReconsError = np.sum(np.square(W - W_inf)) / D
        
        (trainedState, queryState) = train (modelState, X, W, logInterval=1, plotInterval = 100, iterations=130)
        tpcs_inf = rowwise_softmax(safe_log(queryState.expLmda))
        W_inf    = np.array(tpcs_inf.dot(trainedState.vocab) * queryState.docLen[:,np.newaxis], dtype=np.int32)
        
        print ("Model Driven: Prior Reconstruction Error: %f" % (priorReconsError,))
        print ("Model Driven: Final Reconstruction Error: %f" % (np.sum(np.square(W - W_inf)) / D,))
        
        print("End of Test")



if __name__ == "__main__":
    #import sys;sys.argv = ['', 'Test.testName']
    unittest.main()