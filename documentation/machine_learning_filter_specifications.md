# Machine Learning Filter Specifications

## Overview

The Random Forest filter is a machine learning-based component that replaces the traditional 3-stage progressive filtering system for cyber events. It predicts whether raw events should be kept for enrichment based on text features and source metadata.

## Model Architecture

### Model Type
- **Algorithm**: Random Forest Classifier (binary classification)
- **Framework**: scikit-learn
- **Purpose**: Predict `filter_keep` column (1 = keep event, 0 = reject event)

### Model Parameters
- **n_estimators**: 100 trees
- **max_depth**: 5 (minimum depth of 3 to capture interaction effects)
- **min_samples_split**: 10
- **min_samples_leaf**: 5
- **class_weight**: balanced (handles class imbalance)
- **random_state**: 42 (for reproducibility)

## Training Process

### Data Preparation

#### Training Data Source
- **File**: `machine_learning_filter/event_training_data.xlsx` (also available as CSV)
- **Required Columns**:
  - `source_type`: Categorical feature (event source type)
  - `raw_title`: Text feature (event title)
  - `raw_description`: Text feature (event description)
  - `raw_content`: Text feature (event content)
  - `source_url`: Text feature (event URL)
  - `filter_keep`: Target variable (1 = keep, 0 = reject)

#### Feature Engineering

**1. Source Type Encoding**
- Uses `LabelEncoder` to convert categorical source types to numeric values
- Unknown source types default to 0 (first category)
- Saved as: `machine_learning_filter/source_type_encoder.pkl`

**2. URL Preprocessing**
```python
def preprocess_url(url):
    # Remove protocol (https://, http://)
    url = re.sub(r'^https?://', '', str(url))

    # Replace separators with spaces
    url = re.sub(r'[-_]', ' ', url)

    # Remove common URL patterns
    url = re.sub(r'\.(com|org|net|edu|gov|au|uk|us)/?', ' ', url)
    url = re.sub(r'www\.', '', url)

    # Clean up multiple spaces
    url = re.sub(r'\s+', ' ', url).strip()
    return url
```

**3. Text Preprocessing**
```python
def preprocess_text(text):
    # Convert to lowercase
    text = str(text).lower()

    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text
```

**4. Text Vectorization**
- **Method**: TF-IDF (Term Frequency-Inverse Document Frequency)
- **Parameters**:
  - `max_features`: 1000 (limit features to prevent overfitting)
  - `stop_words`: 'english'
  - `ngram_range`: (1, 2) (includes unigrams and bigrams)
  - `min_df`: 2 (ignore terms appearing in < 2 documents)
  - `max_df`: 0.95 (ignore terms appearing in > 95% of documents)
- **Combined Text**: title + description + content + processed_url
- **Saved as**: `machine_learning_filter/text_vectorizer.pkl`

#### Final Feature Matrix
- **Shape**: [n_samples, 1001] (1 categorical + 1000 text features)
- **Features**: [source_type_encoded] + [1000 TF-IDF features]

### Training Pipeline

#### Data Splitting
- **Train/Test Split**: 80/20
- **Stratified**: Yes (maintains class distribution)
- **Random State**: 42

#### Model Training (`train_rf_filter.py`)
```python
# Cross-validation evaluation
cv_scores = cross_val_score(rf_model, X_train, y_train, cv=5, scoring='f1')

# Final model training
rf_model.fit(X_train, y_train)

# Test set evaluation
y_pred = rf_model.predict(X_test)
test_accuracy = accuracy_score(y_test, y_pred)
```

#### Model Evaluation Metrics
- **Cross-validation**: 5-fold F1 score
- **Test metrics**: Accuracy, Precision, Recall, F1-score
- **Feature importance**: Top 20 most important features logged

### Model Artifacts

The training process generates three pickled files:

1. **`random_forest_filter.pkl`**: Trained Random Forest model
2. **`source_type_encoder.pkl`**: Label encoder for source types
3. **`text_vectorizer.pkl`**: TF-IDF vectorizer for text features

## Model Usage for Filtering

### Loading Models (`rf_event_filter.py`)

The `RfEventFilter` class loads all three model artifacts during initialization:

```python
class RfEventFilter:
    def __init__(self, model_dir: str = "machine_learning_filter"):
        self.model = None                    # Random Forest model
        self.source_type_encoder = None      # Label encoder
        self.text_vectorizer = None          # TF-IDF vectorizer
        self._load_models()
```

### Prediction Process

#### Input Parameters
```python
def should_keep_event(self, source_type: str, title: str,
                     description: str = "", content: str = "",
                     url: str = "", metadata: Dict = None) -> FilterResult
```

#### Feature Preparation
1. **URL Preprocessing**: Apply same URL cleaning as training
2. **Text Combination**: Combine all text fields with preprocessing
3. **Source Type Encoding**: Transform source type using trained encoder
4. **Text Vectorization**: Transform combined text using trained TF-IDF vectorizer
5. **Feature Concatenation**: Combine categorical and text features

#### Prediction
```python
# Make binary prediction
prediction = self.model.predict(features)[0]

# Get prediction probabilities
prediction_proba = self.model.predict_proba(features)[0]

# Extract confidence score (probability of positive class)
confidence_score = float(prediction_proba[1])

# Determine if event should be kept
is_cyber_relevant = bool(prediction)
```

#### Output Format
Returns a `FilterResult` object containing:
- `is_cyber_relevant`: Boolean prediction (keep/reject)
- `confidence_score`: Float (0.0-1.0) - probability of positive class
- `reasoning`: List of explanation strings
- `stage`: "rf_filter"
- `risk_level`: "low"/"medium"/"high" based on confidence score

#### Risk Level Mapping
- **High confidence** (≥ 0.8): risk_level = "low"
- **Medium confidence** (0.4-0.8): risk_level = "medium"
- **Low confidence** (< 0.4): risk_level = "high"

### Error Handling

#### Conservative Fallback
If model loading fails or prediction errors occur:
- Default to keeping the event (`is_cyber_relevant = True`)
- Low confidence score (0.3-0.5)
- Error explanation in reasoning
- High risk level

#### Statistics Tracking
The filter maintains runtime statistics:
- `events_processed`: Total events evaluated
- `events_kept`: Events predicted as relevant
- `events_filtered`: Events predicted as irrelevant
- `prediction_errors`: Failed predictions

## Performance Monitoring

### Debugging Tools

#### Model Validation (`debug_rf_filter.py`)
- Compares Random Forest predictions against training data labels
- Calculates accuracy, false positives, and false negatives
- Analyzes specific event patterns causing misclassification
- Provides recommendations for model improvement

#### Filter Optimization (`run_filter_optimization.py`)
- Tests different LLM configurations against training data
- Compares Random Forest vs LLM-based filtering approaches
- Generates performance metrics and detailed analysis reports

### Performance Metrics

#### Key Performance Indicators
- **Accuracy**: Overall correct predictions
- **Precision**: True positives / (True positives + False positives)
- **Recall**: True positives / (True positives + False negatives)
- **F1-Score**: Harmonic mean of precision and recall

#### Expected Performance
Based on cross-validation during training:
- Target accuracy: > 80%
- Balanced precision and recall
- Robust performance across different event types

## Integration Points

### Main Pipeline Integration
The Random Forest filter integrates into the main event discovery pipeline (`discover_enrich_events.py`) as a replacement for the progressive filtering system.

### Compatibility Layer
The `FilterResult` class maintains compatibility with existing filtering interfaces:
- Same return format as progressive filters
- Compatible `risk_level` field
- Consistent logging and statistics interface

## Model Maintenance

### Retraining Triggers
Consider retraining when:
- New event types emerge
- Source type distributions change significantly
- Performance metrics degrade below thresholds
- False negative rate becomes unacceptable

### Training Data Updates
- Add new labeled examples to `event_training_data.xlsx`
- Maintain balanced class distribution
- Include diverse event types and sources
- Re-run `train_rf_filter.py` to update models

### Model Versioning
- Archive previous model artifacts before retraining
- Track performance metrics across model versions
- Implement A/B testing for new models before deployment

## Configuration Options

### Model Directory
- Default: `machine_learning_filter/`
- Configurable via `model_dir` parameter
- Must contain all three model artifacts

### Logging Level
- Debug logging shows individual predictions
- Info logging shows summary statistics
- Warning logging captures prediction errors

### Statistics Management
- Use `reset_statistics()` for new runs
- Use `get_filtering_statistics()` for metrics
- Use `log_filtering_summary()` for reporting