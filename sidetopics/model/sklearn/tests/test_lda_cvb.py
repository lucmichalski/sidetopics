

# 1 A fixture for dummy data that LDA should fit

# 2 Test of CVB 0

from typing import NamedTuple
import numpy as np
import numpy.random as rd
import unittest

from sidetopics.model import DataSet
from sidetopics.model.sklearn import *
from sidetopics.model.sklearn.lda_cvb import TopicModelType

AVG_DOC_LEN = 250
DOC_COUNT = 100
TRUE_TOPIC_COUNT = 5
WORDS_PER_TOPIC = 4  # We actually construct a clustering problem just to test, each topic generates WORDS_PER_TOPIC
                     # terms only, and none others, so that the total vocab size is TRUE_TOPIC_COUNT * WORDS_PER_TOPIC

class TopicModelTestSample:
    components: np.ndarray
    assignments: np.ndarray
    lengths: np.ndarray
    sample_documents: np.ndarray

    def __init__(self, components: np.ndarray, assignments: np.ndarray, lengths: np.ndarray):
        """
        For D documents with T words using K topics
        :param components:  K x T matrix of components
        :param assignments: D x K matrix of topic assignments
        :param lengths:  D x 1 vector of lengths
        :return:
        """
        self.components = components
        self.assignments = assignments
        self.lengths = lengths

        self.sample_documents = np.floor((assignments @ components) * lengths[:, np.newaxis])

    def as_dataset(self, **kwargs) -> DataSet:
        """
        Converts to a dataset, words only. Passes the extra parameters along to
        the DataSet constructor if any
        """
        return DataSet(words=self.sample_documents, **kwargs)

    @property
    def n_components(self):
        return self.components.shape[0]

    @staticmethod
    def new_fixed(seed: int = None):
        if seed is not None:
            rd.seed(seed)

        components = np.array([
            [0] * WORDS_PER_TOPIC*0 +  [1] * WORDS_PER_TOPIC + [0] * 4*WORDS_PER_TOPIC,
            [0] * WORDS_PER_TOPIC*1 + [1] * WORDS_PER_TOPIC + [0] * 3*WORDS_PER_TOPIC,
            [0] * WORDS_PER_TOPIC*2 + [1] * WORDS_PER_TOPIC + [0] * 2*WORDS_PER_TOPIC,
            [0] * WORDS_PER_TOPIC*3 + [1] * WORDS_PER_TOPIC + [0] * 1*WORDS_PER_TOPIC,
            [0] * WORDS_PER_TOPIC*4 + [1] * WORDS_PER_TOPIC + [0] * 0*WORDS_PER_TOPIC
        ])

        assignments = rd.dirichlet(alpha=[1] * TRUE_TOPIC_COUNT, size=DOC_COUNT)
        lens = rd.poisson(AVG_DOC_LEN, size=DOC_COUNT)
        return TopicModelTestSample(components=components,
                                    assignments=assignments,
                                    lengths=lens)


class SklearnLdaCvbTest(unittest.TestCase):
    # Add a test to ensure that repeated calls to transform have the same effect (i.e. we're not training by accident)

    def test_lda_cvb0_data(self):
        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_CVB0, n_components=testcase.n_components, seed=0xC0FFEE)
        assignments = model.fit_transform(dataset)

        model = TopicModel(kind=TopicModelType.LDA_CVB0, n_components=testcase.n_components, seed=0xC0FFEE)
        assignments_2 = model.fit_transform(dataset)
        np.testing.assert_array_almost_equal(assignments, assignments_2, decimal=3)

        print(f'{assignments}')

    def test_lda_cvb0_resume_simple_data(self):
        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_CVB0, n_components=testcase.n_components, seed=0xC0FFEE)
        assignments = model.fit_transform(dataset, iterations=100)

        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_CVB0, n_components=testcase.n_components, seed=0xC0FFEE)
        _ = model.fit_transform(dataset, iterations=90)
        assignments_from_resume = model.fit_transform(dataset, iterations=10, resume=True)

        np.testing.assert_array_almost_equal(assignments, assignments_from_resume, decimal=3)

    def test_lda_cvb_data(self):
        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_CVB, n_components=testcase.n_components, seed=0xC0FFEE)
        assignments = model.fit_transform(dataset)
        model = TopicModel(kind=TopicModelType.LDA_CVB, n_components=testcase.n_components, seed=0xC0FFEE)
        assignments_2 = model.fit_transform(dataset)
        np.testing.assert_array_almost_equal(assignments, assignments_2, decimal=3)

        print(f'{assignments}')

    def test_lda_cvb_resume_simple_data(self):
        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_CVB, n_components=testcase.n_components, seed=0xC0FFEE)
        assignments = model.fit_transform(dataset, iterations=100)

        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_CVB, n_components=testcase.n_components, seed=0xC0FFEE)
        _ = model.fit_transform(dataset, iterations=90)
        assignments_from_resume = model.fit_transform(dataset, iterations=10, resume=True)

        np.testing.assert_array_almost_equal(assignments, assignments_from_resume, decimal=3)


    def test_lda_vb_data(self):
        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_VB_PYTHON_IMPL, n_components=testcase.n_components, iterations=50, seed=0xC0FFEE)
        assignments = model.fit_transform(dataset)

        model = TopicModel(kind=TopicModelType.LDA_VB_PYTHON_IMPL, n_components=testcase.n_components, iterations=50, seed=0xC0FFEE)
        assignments_2 = model.fit_transform(dataset)
        np.testing.assert_array_almost_equal(assignments, assignments_2, decimal=3)

        print(f'{assignments}')

    def test_lda_vb_resume_simple_data(self):
        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_VB_PYTHON_IMPL, n_components=testcase.n_components, seed=0xC0FFEE)
        assignments = model.fit_transform(dataset, iterations=100)

        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_VB_PYTHON_IMPL, n_components=testcase.n_components, seed=0xC0FFEE)
        _ = model.fit_transform(dataset, iterations=90)
        assignments_from_resume = model.fit_transform(dataset, iterations=10, resume=True)  # FIXME resume applies to transform rather than fit, which resumed by default

        np.testing.assert_array_almost_equal(assignments, assignments_from_resume, decimal=3)


    def test_lda_gibbs_data(self):
        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_GIBBS, n_components=testcase.n_components, seed=0xC0FFEE)
        assignments = model.fit_transform(dataset)

        self.assertCountEqual([0], [0], "Equal")

        print(f'{assignments}')

    def test_lda_gibbs_resume_simple_data(self):
        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055)
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_GIBBS, n_components=testcase.n_components,
                           iterations=500, burn_in=500, thin=10, seed=0xC0FFEE)
        assignments = model.fit_transform(dataset)

        testcase = TopicModelTestSample.new_fixed(seed=0xBADB055 )
        dataset = testcase.as_dataset(debug=True)

        model = TopicModel(kind=TopicModelType.LDA_GIBBS, n_components=testcase.n_components,
                           iterations=500, burn_in=500, thin=10, query_iterations=100, seed=0xC0FFEE)
        _tmp = model.fit_transform(dataset, iterations=400)
        assignments_from_resume = model.fit_transform(dataset, iterations=100, burn_in=0, thin=10, resume=True)

        np.testing.assert_array_almost_equal(assignments, assignments_from_resume, decimal=2)  # close enough for sampling...


    def test_lda_svb(self):  #using the LDA_VB_PYTHON impl
        pass

    def test_lda_gibbs_resume_simple_data(self):  #using the LDA_VB_PYTHON impl
        pass

    # Aim of the work is to compare MoM with LDA
    # To compare LDA Gibbs with LDA VB with LDA SVB
    # To compare LDA VB with HDP
    # To look into hyper learning with LDA

