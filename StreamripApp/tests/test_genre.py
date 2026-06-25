import pytest
import numpy as np
from utils.pca_engine import genre_bucket, genre_tokens
from utils.genre_eval import (
    jaccard,
    knn_purity,
    knn_purity_z,
    per_class_auc,
    token_jaccard_agreement,
    compute_silhouette_scores,
)

def test_genre_bucket_regression_guards():
    # Basic matches
    assert genre_bucket("Rap") == "Hip-Hop"
    assert genre_bucket("Hip hop") == "Hip-Hop"
    
    # French and Russian Cyrillic translations
    assert genre_bucket("Хип-хоп") == "Hip-Hop"
    assert genre_bucket("Рэп") == "Hip-Hop"
    assert genre_bucket("Électronique") == "Electronic"
    assert genre_bucket("classique") == "Classical"
    assert genre_bucket("рок") == "Rock/Alt"
    assert genre_bucket("поп") == "Pop"
    
    # Priority order matching (rare/failing matched before rock/pop)
    # "Pop, Rock, Metal" -> Metal
    assert genre_bucket("Pop, Rock, Metal") == "Metal"
    # "Pop, Rock" -> Rock/Alt (since Rock/Alt matches before Pop)
    assert genre_bucket("Pop, Rock") == "Rock/Alt"


def test_genre_tokens():
    # Multi-label sets
    assert genre_tokens("Pop, Rock, Metal") == {"Metal", "Rock/Alt", "Pop"}
    assert genre_tokens("Rap & House") == {"Hip-Hop", "Electronic"}
    
    # FR/RU translations
    assert genre_tokens("électro & рэп") == {"Electronic", "Hip-Hop"}
    
    # Fallback and empty
    assert genre_tokens("") == {"Unknown"}
    assert genre_tokens(None) == {"Unknown"}
    assert genre_tokens("Unrecognized Genre String") == {"Other"}


def test_jaccard():
    assert jaccard({"A", "B"}, {"B", "C"}) == 0.3333333333333333
    assert jaccard({"A"}, {"A"}) == 1.0
    assert jaccard(set(), set()) == 1.0
    assert jaccard({"A"}, set()) == 0.0


def test_knn_purity_metrics():
    # Simple 4-point case
    # Labels: 0->A, 1->A, 2->B, 3->B
    # Order: each point has its closest neighbors
    labels = np.array(["A", "A", "B", "B"])
    valid = np.array([True, True, True, True])
    
    # If order is perfect:
    # 0's neighbors: [0, 1, 2, 3] -> 1st valid neighbor is 1 (matches A)
    # 1's neighbors: [1, 0, 2, 3] -> 1st valid neighbor is 0 (matches A)
    # 2's neighbors: [2, 3, 0, 1] -> 1st valid neighbor is 3 (matches B)
    # 3's neighbors: [3, 2, 0, 1] -> 1st valid neighbor is 2 (matches B)
    order = np.array([
        [0, 1, 2, 3],
        [1, 0, 2, 3],
        [2, 3, 0, 1],
        [3, 2, 0, 1]
    ])
    
    # Overall purity with k=1
    overall, class_purities = knn_purity(order, labels, valid, k=1)
    assert overall == 1.0
    assert class_purities["A"][0] == 1.0
    assert class_purities["B"][0] == 1.0

    # Test knn_purity_z
    obs, null_mean, null_std, z = knn_purity_z(order, labels, valid, k=1, B=20, seed=0)
    assert obs == 1.0
    assert z > 0.0


def test_token_jaccard_agreement():
    token_sets = [{"A", "B"}, {"B", "C"}, {"D"}, {"D"}]
    valid = np.array([True, True, True, True])
    order = np.array([
        [0, 1, 2, 3], # 0's closest is 1: jaccard({"A","B"}, {"B","C"}) = 1/3
        [1, 0, 2, 3], # 1's closest is 0: jaccard = 1/3
        [2, 3, 0, 1], # 2's closest is 3: jaccard({"D"}, {"D"}) = 1.0
        [3, 2, 0, 1]  # 3's closest is 2: jaccard = 1.0
    ])
    obs, null_mean, null_std, z = token_jaccard_agreement(order, token_sets, valid, k=1, B=20, seed=0)
    expected_obs = (1/3 + 1/3 + 1.0 + 1.0) / 4.0
    assert abs(obs - expected_obs) < 1e-7


def test_per_class_auc():
    # 2 classes: A and B
    # 4 points: 0, 1 (Class A), 2, 3 (Class B)
    # Distances from 0: to 1 is 1.0, to 2 is 2.0, to 3 is 3.0 (perfectly separates A and B)
    # Distances from 1: to 0 is 1.0, to 2 is 2.0, to 3 is 3.0
    # Distances from 2: to 3 is 1.0, to 0 is 2.0, to 1 is 3.0
    # Distances from 3: to 2 is 1.0, to 0 is 2.0, to 1 is 3.0
    D = np.array([
        [0.0, 1.0, 2.0, 3.0],
        [1.0, 0.0, 2.0, 3.0],
        [2.0, 3.0, 0.0, 1.0],
        [2.0, 3.0, 1.0, 0.0]
    ])
    labels = np.array(["A", "A", "B", "B"])
    valid = np.array([True, True, True, True])
    
    # Calculate AUC with min_n=2
    auc_res = per_class_auc(D, labels, valid, min_n=2)
    # Class A positives: from 0, pos is 1 (dist=1.0). negs are 2 (dist=2.0), 3 (dist=3.0).
    # Since 1.0 < 2.0 and 1.0 < 3.0, AUC for 0 is 1.0. Similarly for all other points.
    assert auc_res["A"][0] == 1.0
    assert auc_res["B"][0] == 1.0


def test_compute_silhouette_scores():
    D = np.array([
        [0.0, 1.0, 10.0, 10.0],
        [1.0, 0.0, 10.0, 10.0],
        [10.0, 10.0, 0.0, 1.0],
        [10.0, 10.0, 1.0, 0.0]
    ])
    labels = np.array(["A", "A", "B", "B"])
    valid = np.array([True, True, True, True])
    sil, global_sil, class_sils = compute_silhouette_scores(D, labels, valid)
    # For point 0: a = 1.0, b = 10.0. sil = (10 - 1) / 10 = 0.9
    assert abs(sil[0] - 0.9) < 1e-7
    assert abs(global_sil - 0.9) < 1e-7
    assert class_sils["A"][0] == 0.9
