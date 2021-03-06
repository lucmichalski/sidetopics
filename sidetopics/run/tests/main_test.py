'''
Created on 27 Nov 2013

@author: bryanfeeney
'''
import unittest
import pickle as pkl
import tempfile as tmp
import cProfile

from model_test.stm_yv_test import sampleFromModel
from run.main import run, ModelNames, \
    Rtm, LdaGibbs, LdaVb, Mtm, Mtm2, StmYvBohning, StmYvBouchard, \
    CtmBohning, CtmBouchard, Dmr, StmYvBohningFakeOnline, Lro, \
    SimLda, SimTfIdf, LdaSvb, StmUyvBohning, MomEm, MomGibbs, LdaCvb, LdaCvbZero

from model.evals import Perplexity, MeanAveragePrecAllDocs, \
    MeanPrecRecAtMAllDocs, HashtagPrecAtM, TagPrecAtM, \
    LroMeanPrecRecAtMAllDocs, LroMeanPrecRecAtMFeatSplit

AclPath = "/Users/bryanfeeney/iCloud/Datasets/ACL/ACL.100.clean/"
_AclWordPath  = AclPath + "words-freq.pkl"
_AclDictPath  = AclPath + "words-freq-dict.pkl"
_AclFeatsPath = AclPath + "feats.pkl"
_AclCitePath  = AclPath + "ref.pkl"

NipsPath = "/Users/bryanfeeney/iCloud/Datasets/NIPS-from-pryor-Sep15/"
_NipsWordPath = NipsPath + "W_ar.pkl"
_NipsFeatPath = NipsPath + "X_ar.pkl"
_NipsDictPath = None

ReutersPath = "/Users/bryanfeeney/Desktop/reuters/"
_ReutersWordPath = ReutersPath + "W.pkl"
_ReutersDictPath = ReutersPath + "dict.pkl"

Tweets1100Path = "/Users/bryanfeeney/iCloud/Datasets/Tweets/Tweets-1.1m/"
_Tweets1100WordPath = Tweets1100Path + "words.pkl"
_Tweets1100FeatPath = Tweets1100Path + "side.pkl"

_AuthorTweets1100WordPath     = Tweets1100Path + "words-by-author.pkl"
_AuthorTweets1100FreqWordPath = Tweets1100Path + "words-by-author-freq.pkl"
_Tweets1100FreqWordPath       = Tweets1100Path + "words-freq.pkl"
_Tweets1100FreqDictPath       = Tweets1100Path + "words-freq-dict.pkl"


Tweets2900Path = "/Users/bryanfeeney/iCloud/Datasets/Tweets/Tweets-2.9m/"
_Tweets2900WordPath = Tweets2900Path + "words.pkl"
_Tweets2900FeatPath = Tweets2900Path + "side.pkl"

_AuthorTweets2900WordPath     = Tweets2900Path + "words-by-author.pkl"
_AuthorTweets2900FreqWordPath = Tweets2900Path + "words-by-author-freq.pkl"
_Tweets2900FreqWordPath       = Tweets2900Path + "words-freq.pkl"
_Tweets2900FreqDictPath       = Tweets2900Path + "words-freq-dict.pkl"


Tweets750Path = "/Users/bryanfeeney/iCloud/Datasets/Tweets/Cluster2015-06-24/AuthorTime750/"
_Tweets750WordPath = Tweets750Path + "words-cleaned.pkl"
_Tweets750FeatPath = Tweets750Path + "side-cleaned.pkl"

_AuthorTweets750WordPath     = Tweets750Path + "words-by-author.pkl"
_AuthorTweets750FreqWordPath = Tweets750Path + "words-by-author-freq.pkl"
_Tweets750FreqWordPath       = Tweets750Path + "words-cleaned-freq.pkl"
_Tweets750FreqDictPath       = Tweets750Path + "words-cleaned-freq-dict.pkl"

Tweets800Path = "/Users/bryanfeeney/Desktop/SmallerDB-NoCJK-WithFeats-Fixed/"
_Tweets800WordPath = Tweets800Path + "words.pkl"
_Tweets800FeatPath = Tweets800Path + "side.pkl"

_AuthorTweets800WordPath     = Tweets800Path + "words-by-author.pkl"
_AuthorTweets800FreqWordPath = Tweets800Path + "words-by-author-freq.pkl"
_Tweets800FreqWordPath       = Tweets800Path + "words-freq.pkl"
_Tweets800FreqWordPath       = Tweets800Path + "words-freq-dict.pkl"

Tweets500Path = "/Users/bryanfeeney/iCloud/Datasets/Tweets/AuthorTime/"
_Tweets500WordPath = Tweets500Path + "words.pkl"
_Tweets500FeatPath = Tweets500Path + "side.pkl"

_Author500TweetsWordPath     = Tweets500Path + "words-by-author.pkl"
_Author500TweetsFreqWordPath = Tweets500Path + "words-by-author-freq.pkl"
_Tweets500FreqWordPath       = Tweets500Path + "words-cleaned-freq.pkl"
_Tweets500FreqDictPath       = Tweets500Path + "words-cleaned-freq-dict.pkl"

 # Pick either 500 or 750 or 800 or 1100 or 2900
_TweetsWordPath = _Tweets500WordPath
_TweetsFeatPath = _Tweets500FeatPath

_AuthorTweetsWordPath     = _Author500TweetsWordPath
_AuthorTweetsFreqWordPath = _Author500TweetsFreqWordPath
_TweetsFreqWordPath       = _Tweets500FreqWordPath
_TweetsFreqDictPath       = _Tweets500FreqDictPath


_TrumpTweetsPath = "/Users/bryanfeeney/Desktop/TrumpDb/"
_TrumpTweetsWords = _TrumpTweetsPath + "word-freq.pkl"
_TrumpTweetsFeats = _TrumpTweetsPath + "side-freq.pkl"
_TrumpTweetsDicts = _TrumpTweetsPath + "worddict-freq.pkl"

_NusWidePath  = "/Users/bryanfeeney/iCloud/Datasets/NusWide/"
_NusWideWords = _NusWidePath + "W5K-ld.pkl"
_NusWideFeats = _NusWidePath + "X-ld.pkl"
_NusWideDicts = _NusWidePath + "dict5K.pkl"

_NusWideImgPath  = _NusWidePath
_NusWideImgWords = _NusWidePath + "W5K-ld-images.pkl"
_NusWideImgFeats = _NusWidePath + "X-ld-images.pkl"
_NusWideImgDicts = _NusWideDicts

NusWideImg, NusWide, TrumpTweets, Reuters, Acl, AclNoLinks, TweetsAll, TweetsFreq, AuthorTweetsAll, AuthorTweetsFreq, Nips = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
WordsPath = [_NusWideImgWords, _NusWideWords, _TrumpTweetsWords, _ReutersWordPath, _AclWordPath,  _AclWordPath,  _TweetsWordPath, _TweetsFreqWordPath, _AuthorTweetsWordPath, _AuthorTweetsFreqWordPath, _NipsWordPath]
FeatsPath = [_NusWideImgFeats, _NusWideFeats, _TrumpTweetsFeats, None,             _AclFeatsPath, _AclFeatsPath, _TweetsFeatPath, _TweetsFeatPath,     None,                  None,                      _NipsFeatPath]
CitesPath = [None,             None,          None,              None,             _AclCitePath,  None,          None,            None,                None,                  None,                      None]
DictsPath = [_NusWideImgDicts, _NusWideDicts, _TrumpTweetsDicts, _ReutersDictPath, _AclDictPath,  _AclDictPath,  None,            _TweetsFreqDictPath, None,                  _TweetsFreqDictPath,       _NipsDictPath]

PreBuildTopicsDir="/Users/bryanfeeney/Desktop/acl-out-tm/lda/"
PreBuiltAclGibbsTopics = { \
     10 : PreBuildTopicsDir + "lda_gibbs_k_10_pkl", \
     25 : PreBuildTopicsDir + "lda_gibbs_k_25.pkl", \
     50 : PreBuildTopicsDir + "lda_gibbs_k_50.pkl", \
     75 : PreBuildTopicsDir + "lda_gibbs_k_75.pkl", \
    100 : PreBuildTopicsDir + "lda_gibbs_k_100.pkl", \
    150 : PreBuildTopicsDir + "lda_gibbs_k_150.pkl"
}
PreBuiltAclVbTopics = { \
      10 : PreBuildTopicsDir + "lda_vbp_k_10.pkl", \
      25 : PreBuildTopicsDir + "lda_vbp_k_25.pkl", \
      50 : PreBuildTopicsDir + "lda_vbp_k_50.pkl", \
      75 : PreBuildTopicsDir + "lda_vbp_k_75.pkl", \
     100 : PreBuildTopicsDir + "lda_vbp_k_100.pkl", \
     150 : PreBuildTopicsDir + "lda_vbp_k_150.pkl"
}
PreBuiltGibbsTopics = { Acl : PreBuiltAclGibbsTopics }
PreBuiltVbTopics = { Acl : PreBuiltAclVbTopics }
FeatsMask = { Acl : "2001:1088,2002:1085,2003:1086,2004:1083,2005:1084,2006:1081" }


def tmpFiles():
    '''
    Returns files in the temporary directory for storing the DxT matrix of
    word counts, the DxF matrix of features, the file which stores the model
    details, and the file containing a plot of the variational bounds.
    '''
    tmpDir = tmp.gettempdir()
    return tmpDir + '/words.pkl', tmpDir + '/feats.pkl', tmpDir

class Test(unittest.TestCase):


    def setUp(self):
        pass


    def tearDown(self):
        pass


    def testMainScript(self):
        D, T, K, Q, F, P, avgWordsPerDoc = 200, 100, 10, 6, 12, 8, 500
        tpcs, vocab, docLens, X, W = sampleFromModel(D, T, K, F, P, avgWordsPerDoc)
        
        wordsFile, featsFile, modelFileDir = tmpFiles()
        with open(wordsFile, 'wb') as f:
            pkl.dump(W, f)
        with open(featsFile, 'wb') as f:
            pkl.dump(X, f)
        
        print ("New Version")

        RateDelay       = 10
        ForgettingRate  = 0.55
        BatchSize       = 100

        sgd_setups = [(b,r,f) for b in [1, 5, 10, 100] for r in [1, 10, 30] for f in [0.6, 0.75, 0.9]]

        Folds, ExecutedFoldCount = 5,1
        K,P,Q = 10, 150, 20
        TrainIters, QueryIters, LogFreq = 2, 2, 1

        PriorCov = 1
        VocabPrior = 5
        Debug = True

        modelFileses = []
        for DataSetName in [TweetsFreq]:
            for k in [K]: # [10, 25, 50, 100]:
                for p in [P]: #, [50, 100, 250, 500]:
                    #for (BatchSize, RetardationRate, ForgettingRate) in sgd_setups:
                    for modelName in [ StmYvBohning ]:
                        cmdline = '' \
                                +(' --debug '          + str(Debug) if Debug else "") \
                                + ' --model '          + modelName \
                                + ' --dtype '          + 'f8'      \
                                + ' --num-topics '     + str(k)    \
                                + ' --num-lat-feats '  + str(p) \
                                + ' --num-lat-topics ' + str(Q) \
                                + ' --log-freq '       + str(LogFreq)       \
                                + ' --eval '           + HashtagPrecAtM  \
                                + ' --gradient-batch-size '      + str(BatchSize) \
                                + ' --gradient-rate-delay '      + str(RateDelay) \
                                + ' --gradient-forgetting-rate ' + str(ForgettingRate) \
                                + ' --iters '          + str(TrainIters)      \
                                + ' --query-iters '    + str(QueryIters)      \
                                + ' --folds '          + str(Folds)      \
                                + ' --truncate-folds ' + str(ExecutedFoldCount)      \
                                + (' --word-dict '     + DictsPath[DataSetName] if DictsPath[DataSetName] is not None else "") \
                                + ' --words '          + WordsPath[DataSetName] \
                                + (' --feats '         + FeatsPath[DataSetName] if FeatsPath[DataSetName] is not None else "") \
                                + (' --links '         + CitesPath[DataSetName] if CitesPath[DataSetName] is not None else "") \
                                + ' --topic-var '      + str(PriorCov) \
                                + ' --feat-var '       + str(PriorCov) \
                                + ' --lat-topic-var '  + str(PriorCov) \
                                + ' --lat-feat-var '   + str(PriorCov) \
                                + ' --vocab-prior '    + str(VocabPrior) \
                                + ' --tag-recall-opts ' + "0:-1,0.75"
            #                     + ' --out-model '      + '/Users/bryanfeeney/Desktop/acl-out-tm' \
            #                    + ' --feats-mask '     + FeatsMask[DataSetName] \
            #                    + ' --lda-model '      + PreBuiltVbTopics[DataSetName][k]
            #                    + ' --words '          + '/Users/bryanfeeney/Dropbox/Datasets/ACL/words.pkl' \
            #                    + ' --words '          + '/Users/bryanfeeney/Desktop/NIPS-from-pryor-Sep15/W_ar.pkl'
            #                    + ' --words '          + '/Users/bryanfeeney/Desktop/Dataset-Sep-2014/words.pkl' \
            #                    + ' --feats '          + '/Users/bryanfeeney/Desktop/Dataset-Sep-2014/side.pkl'
            #                    + ' --words '          + wordsFile \
            #                    + ' --feats '          + featsFile
            #                    + ' --words '          + '/Users/bryanfeeney/Desktop/Tweets600/words-by-author.pkl' \

                        modelFileses.extend (run(cmdline.strip().split(' ')))

                    modelFileses.insert(0, wordsFile)
                    modelFileses.insert(1, featsFile)
                    print ("Files can be found in:" + "\n\t".join(modelFileses))
        
    
    def _testLoadResult(self):
        path = "/Users/bryanfeeney/Desktop/out.sample/ctm_bouchard_k_50_20140223_1719.pkl"
        with open (path, 'rb') as f:
            (order, boundItrses, boundValses, models, trainTopicses, queryTopicses) = pkl.load(f)
        print (str(boundItrses[0]))
        print (models[0].name)

if __name__ == "__main__":
    import sys;sys.argv = ['', 'Test.testUyv']
    unittest.main()

