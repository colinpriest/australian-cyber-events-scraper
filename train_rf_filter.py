#!/usr/bin/env python3
"""
Random Forest Filter Training Script

This script trains a binary classification Random Forest model to predict the filter_keep column
using source_type, raw_title, raw_description, raw_content, and source_url features.

Features:
- source_type: categorical column
- raw_title, raw_description, raw_content, source_url: text features requiring NLP preprocessing
- URL preprocessing: removes https://, replaces - and _ with spaces
- Cross-validation for robust model evaluation
- Minimum tree depth of 3 to capture interaction effects
"""

from __future__ import annotations

import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
import pickle
import os
import re
from pathlib import Path
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def preprocess_url(url):
    """
    Preprocess URL by removing protocol and replacing separators with spaces.
    
    Args:
        url (str): Original URL
        
    Returns:
        str: Preprocessed URL text
    """
    if pd.isna(url) or not url:
        return ""
    
    # Remove protocol
    url = re.sub(r'^https?://', '', str(url))
    
    # Replace separators with spaces
    url = re.sub(r'[-_]', ' ', url)
    
    # Remove common URL patterns that don't add meaning
    url = re.sub(r'\.(com|org|net|edu|gov|au|uk|us)/?', ' ', url)
    url = re.sub(r'www\.', '', url)
    
    # Clean up multiple spaces
    url = re.sub(r'\s+', ' ', url).strip()
    
    return url

def preprocess_text(text):
    """
    Basic text preprocessing for NLP features.
    
    Args:
        text (str): Original text
        
    Returns:
        str: Preprocessed text
    """
    if pd.isna(text) or not text:
        return ""
    
    # Convert to string and lowercase
    text = str(text).lower()
    
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

def load_and_preprocess_data(file_path):
    """
    Load training data from Excel file and preprocess features.
    
    Args:
        file_path (str): Path to the Excel file
        
    Returns:
        tuple: (X_features, y_target, feature_names)
    """
    logger.info(f"Loading training data from {file_path}")
    
    # Load the Excel file
    df = pd.read_excel(file_path)
    logger.info(f"Loaded {len(df)} training samples")
    
    # Check required columns
    required_columns = ['source_type', 'raw_title', 'raw_description', 'raw_content', 'source_url', 'filter_keep']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")
    
    # Check target distribution
    target_dist = df['filter_keep'].value_counts()
    logger.info(f"Target distribution:\n{target_dist}")
    
    # Preprocess features
    logger.info("Preprocessing features...")
    
    # 1. Categorical feature: source_type
    le_source_type = LabelEncoder()
    source_type_encoded = le_source_type.fit_transform(df['source_type'].fillna('unknown'))
    
    # 2. Text features: combine and preprocess
    # Preprocess URL
    df['processed_url'] = df['source_url'].apply(preprocess_url)
    
    # Combine all text features
    df['combined_text'] = (
        df['raw_title'].fillna('').apply(preprocess_text) + ' ' +
        df['raw_description'].fillna('').apply(preprocess_text) + ' ' +
        df['raw_content'].fillna('').apply(preprocess_text) + ' ' +
        df['processed_url'].fillna('').apply(preprocess_text)
    )
    
    # Vectorize text features
    logger.info("Vectorizing text features...")
    vectorizer = TfidfVectorizer(
        max_features=1000,  # Limit features to prevent overfitting
        stop_words='english',
        ngram_range=(1, 2),  # Include bigrams
        min_df=2,  # Ignore terms that appear in less than 2 documents
        max_df=0.95  # Ignore terms that appear in more than 95% of documents
    )
    
    text_features = vectorizer.fit_transform(df['combined_text'])
    
    # Combine categorical and text features
    X_features = np.hstack([
        source_type_encoded.reshape(-1, 1),
        text_features.toarray()
    ])
    
    # Target variable
    y_target = df['filter_keep'].values
    
    # Feature names for interpretability
    feature_names = ['source_type'] + vectorizer.get_feature_names_out().tolist()
    
    logger.info(f"Final feature matrix shape: {X_features.shape}")
    logger.info(f"Number of text features: {text_features.shape[1]}")
    
    return X_features, y_target, feature_names, le_source_type, vectorizer

def train_random_forest(X, y, feature_names):
    """
    Train Random Forest classifier with cross-validation.
    
    Args:
        X (np.array): Feature matrix
        y (np.array): Target variable
        feature_names (list): Names of features
        
    Returns:
        RandomForestClassifier: Trained model
    """
    logger.info("Training Random Forest classifier...")
    
    # Split data for final evaluation
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )
    
    # Create Random Forest with specified parameters
    rf_model = RandomForestClassifier(
        n_estimators=100,
        max_depth=5,  # Minimum depth of 3, using 5 for better performance
        min_samples_split=10,
        min_samples_leaf=5,
        random_state=42,
        n_jobs=-1,  # Use all available cores
        class_weight='balanced'  # Handle class imbalance
    )
    
    # Cross-validation
    logger.info("Performing cross-validation...")
    cv_scores = cross_val_score(rf_model, X_train, y_train, cv=5, scoring='f1')
    logger.info(f"Cross-validation F1 scores: {cv_scores}")
    logger.info(f"Mean CV F1 score: {cv_scores.mean():.3f} (+/- {cv_scores.std() * 2:.3f})")
    
    # Train final model
    rf_model.fit(X_train, y_train)
    
    # Evaluate on test set
    y_pred = rf_model.predict(X_test)
    test_accuracy = accuracy_score(y_test, y_pred)
    logger.info(f"Test set accuracy: {test_accuracy:.3f}")
    
    # Detailed classification report
    logger.info("Classification Report:")
    logger.info(f"\n{classification_report(y_test, y_pred)}")
    
    # Feature importance
    feature_importance = pd.DataFrame({
        'feature': feature_names,
        'importance': rf_model.feature_importances_
    }).sort_values('importance', ascending=False)
    
    logger.info("Top 20 most important features:")
    logger.info(feature_importance.head(20).to_string(index=False))
    
    return rf_model

def save_model_and_preprocessors(model, le_source_type, vectorizer, output_dir):
    """
    Save the trained model and preprocessing components.
    
    Args:
        model: Trained Random Forest model
        le_source_type: Label encoder for source_type
        vectorizer: TF-IDF vectorizer for text features
        output_dir (str): Directory to save models
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)
    
    logger.info(f"Saving models to {output_dir}")
    
    # Save Random Forest model
    model_path = output_dir / 'random_forest_filter.pkl'
    with open(model_path, 'wb') as f:
        pickle.dump(model, f)
    
    # Save label encoder
    le_path = output_dir / 'source_type_encoder.pkl'
    with open(le_path, 'wb') as f:
        pickle.dump(le_source_type, f)
    
    # Save vectorizer
    vectorizer_path = output_dir / 'text_vectorizer.pkl'
    with open(vectorizer_path, 'wb') as f:
        pickle.dump(vectorizer, f)
    
    logger.info(f"Models saved successfully:")
    logger.info(f"  - Random Forest: {model_path}")
    logger.info(f"  - Source Type Encoder: {le_path}")
    logger.info(f"  - Text Vectorizer: {vectorizer_path}")

def main():
    """Main training pipeline."""
    # File paths
    data_file = "machine_learning_filter/event_training_data.xlsx"
    output_dir = "machine_learning_filter"
    
    try:
        # Load and preprocess data
        X, y, feature_names, le_source_type, vectorizer = load_and_preprocess_data(data_file)
        
        # Train model
        model = train_random_forest(X, y, feature_names)
        
        # Save models
        save_model_and_preprocessors(model, le_source_type, vectorizer, output_dir)
        
        logger.info("Training completed successfully!")
        
    except Exception as e:
        logger.error(f"Training failed: {e}")
        raise

if __name__ == "__main__":
    main()
