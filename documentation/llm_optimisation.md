# LLM Optimization Documentation

## Overview

This document describes the comprehensive LLM (Large Language Model) optimization process used to ensure irrelevant articles are filtered out while relevant cybersecurity incidents are kept. The optimization involves systematic prompt engineering, rule development, and performance validation against labeled training data.

## Architecture Overview

### Components
- **Base LLM Classifier** (`llm_classifier.py`): Production classifier using OpenAI GPT models
- **Experimental LLM Classifier** (`experimental_llm_classifier.py`): Extended classifier for prompt optimization
- **Filter Tuner** (`LLMFilterTuner`): Systematic testing framework for prompt variants
- **Optimization Pipeline** (`run_filter_optimization.py`): Comprehensive optimization workflow

### Integration Points
- **Primary**: Event classification in the discovery pipeline
- **Training**: Performance evaluation against labeled training data
- **Validation**: Quick testing scripts for prompt validation

## Prompt Engineering Strategy

### Prompt Variants

The system implements four distinct prompt variants, each optimized for different filtering characteristics:

#### 1. Default Prompt (`_get_default_prompt`)
**Purpose**: Balanced precision and recall for production use

**Key Features**:
- Conservative approach with explicit validation steps
- Focus on "ONE SPECIFIC cybersecurity INCIDENT"
- Automatic rejection of summaries and policy documents
- Detailed confidence scoring requirements

**Critical Rules**:
```
- Return TRUE ONLY for: actual data breaches, cyber attacks, malware infections,
  ransomware attacks, phishing campaigns that OCCURRED to ONE specific named company
- Return FALSE for: general summaries, policy documents, training materials,
  multiple incidents, trend reports
```

**System Prompt**: "You are a strict cybersecurity incident analyst. ONLY classify events that are genuine cybersecurity INCIDENTS where actual damage, compromise, or breach occurred to ONE SPECIFIC NAMED ORGANIZATION."

#### 2. Strict Prompt (`_get_strict_prompt`)
**Purpose**: Maximum precision with very conservative filtering

**Enhanced Requirements**:
- All five critical requirements must be TRUE
- Automatic rejection list for 8+ categories
- High confidence threshold (>0.8) required
- Extensive evidence requirements

**Automatic Rejection Categories**:
- Multiple incident indicators: "multiple", "several", "various"
- Policy documents: "frameworks", "guidelines", "recommendations"
- Educational content: "training materials", "best practices"
- Market analysis: "reports", "trend analyses", "statistics"
- Time period summaries: "2020 breaches", "Q1 incidents"
- Government guidance: "regulatory updates"
- Future planning: "planning documents"

#### 3. Lenient Prompt (`_get_lenient_prompt`)
**Purpose**: Higher recall with more permissive classification

**Relaxed Criteria**:
- Accept any cybersecurity-related content
- Include potential security implications
- Generous confidence scoring
- Broader Australian connection acceptance

**Acceptance Rules**:
- Any cybersecurity incident, breach, or attack
- Australian organizations in security context
- Specific security events or investigations
- Actual security compromises or threats

#### 4. Detailed Prompt (`_get_detailed_prompt`)
**Purpose**: Comprehensive analysis with extensive evidence collection

**Analysis Framework**:
1. **Content Analysis**: Cybersecurity terms, narrative structure, specificity
2. **Incident Specificity**: Single incident, named organizations, actual events
3. **Australian Relevance**: Direct mentions, geographic references, regulatory context
4. **Confidence Assessment**: Detailed breakdown by category
5. **Evidence Collection**: Comprehensive indicator lists

### Confidence Scoring System

All prompts implement a multi-dimensional confidence scoring system:

- **Content Confidence** (0.0-1.0): Strength of cybersecurity indicators
- **Specificity Confidence** (0.0-1.0): Evidence of specific incident vs. general content
- **Australian Confidence** (0.0-1.0): Strength of Australian connection
- **Overall Confidence** (0.0-1.0): Combined assessment

### Evidence Collection

Each classification includes detailed evidence lists:
- **Detected Indicators**: Specific cybersecurity indicators found
- **Rejection Indicators**: Reasons for rejection (if applicable)
- **Australian Indicators**: Evidence of Australian relevance

## Rule Optimization Methodology

### Training Data Analysis

#### Data Characteristics
- **Source**: `machine_learning_filter/event_training_data.xlsx`
- **Size**: Labeled dataset with `filter_keep` column (1=keep, 0=reject)
- **Balance**: Tracks keep/reject ratio for balanced evaluation

#### Pattern Identification

**Events to KEEP (Specific Incidents)**:
- Named organizations (Toll Group, Perth Mint)
- Individual incidents with past tense descriptions
- Actual compromise details
- Specific technical details

**Events to REJECT (General Content)**:
- Multiple incident summaries with keywords: "Multiple", "Various", "Several"
- Time period descriptions: "January 2020", "Q1 2020"
- Regulatory reports: OAIC summaries, government guidance
- Policy documents: frameworks, action plans, guidelines
- Educational content: training materials, best practices

### Systematic Testing Framework

#### Configuration Testing (`run_filter_optimization.py`)

**Test Matrix**:
```python
configurations = [
    {"prompt_variant": "default", "model": "gpt-4o-mini", "confidence_threshold": 0.3},
    {"prompt_variant": "default", "model": "gpt-4o-mini", "confidence_threshold": 0.5},
    {"prompt_variant": "default", "model": "gpt-4o-mini", "confidence_threshold": 0.7},
    {"prompt_variant": "strict", "model": "gpt-4o-mini", "confidence_threshold": 0.3},
    {"prompt_variant": "strict", "model": "gpt-4o-mini", "confidence_threshold": 0.5},
    {"prompt_variant": "lenient", "model": "gpt-4o-mini", "confidence_threshold": 0.3},
    {"prompt_variant": "lenient", "model": "gpt-4o-mini", "confidence_threshold": 0.5},
    {"prompt_variant": "detailed", "model": "gpt-4o-mini", "confidence_threshold": 0.5}
]
```

#### Performance Metrics

**Primary Metrics**:
- **Precision**: TP / (TP + FP) - Accuracy of positive predictions
- **Recall**: TP / (TP + FN) - Coverage of actual positives
- **F1 Score**: 2 * (precision * recall) / (precision + recall) - Balanced metric
- **Accuracy**: (TP + TN) / Total - Overall correctness

**Secondary Metrics**:
- **Average Confidence**: Mean confidence score across predictions
- **True/False Positive/Negative Counts**: Detailed confusion matrix

#### Content Enrichment Process

**Web Scraping Enhancement**:
1. **URL Processing**: Fetch content from source URLs
2. **Content Extraction**: BeautifulSoup-based text extraction
3. **Content Cleaning**: Remove scripts, styles, navigation elements
4. **Content Limiting**: Truncate to 2000 characters for LLM processing

**Error Handling**:
- **Scraping Failures**: Fall back to raw description + content
- **LLM Errors**: Conservative approach - default to keeping events
- **Timeout Management**: 30-second timeout for web requests

## Validation and Testing

### Quick Validation (`quick_filter_test.py`)

**Purpose**: Rapid validation of prompt changes with key test cases

**Test Cases**:
```python
test_cases = [
    {
        "title": "Toll Group Ransomware Attack",
        "expected": True,
        "description": "Specific incident - should KEEP"
    },
    {
        "title": "Multiple Cyber Incidents Reported in Australia (January 2020)",
        "expected": False,
        "description": "General summary - should REJECT"
    },
    {
        "title": "Australian Data Breach Action Plan Framework",
        "expected": False,
        "description": "Policy framework - should REJECT"
    }
]
```

### Comprehensive Testing (`test_llm_filter_tuning.py`)

**Features**:
- **Full Dataset Processing**: Tests against complete training dataset
- **Web Content Scraping**: Real-time content fetching for URLs
- **Detailed Metrics**: Comprehensive performance analysis
- **Error Analysis**: False positive/negative investigation
- **Result Export**: CSV export for detailed analysis

### Performance Monitoring

#### Debug Analysis (`debug_rf_filter.py`)

**Comparison Testing**:
- Random Forest vs. LLM classification comparison
- Training data vs. production prediction analysis
- Pattern analysis for misclassifications

#### Filter Tuning Summary (`filter_tuning_summary.py`)

**Automated Analysis**:
- Training data pattern identification
- Key example testing
- Performance recommendations
- Usage instructions

## Optimization Results

### Key Findings

#### Pattern Recognition Success
- **Specific Incidents**: Successfully identifies named organization breaches
- **Summary Rejection**: Effectively filters multi-incident reports
- **Policy Filtering**: Reliably rejects framework and guidance documents
- **Time Period Filtering**: Correctly identifies and rejects period summaries

#### Optimal Configuration
- **Prompt Variant**: "default" provides best balance
- **Confidence Threshold**: 0.5 recommended for production
- **Model**: GPT-4o-mini provides cost-effective performance

#### Performance Benchmarks
- **Target Accuracy**: >80% on validation set
- **False Positive Rate**: <10% (irrelevant events kept)
- **False Negative Rate**: <20% (relevant events rejected)

### Threshold Optimization

#### Confidence Threshold Effects
- **0.3**: High recall, more false positives
- **0.5**: Balanced precision/recall (recommended)
- **0.7**: High precision, more false negatives

#### Production Recommendations
1. **Standard Use**: threshold=0.5 with default prompt
2. **High Precision Needs**: threshold=0.7 with strict prompt
3. **High Recall Needs**: threshold=0.3 with lenient prompt

## Implementation Guidelines

### Setup Requirements

#### Dependencies (`requirements_filter_tuning.txt`)
```
beautifulsoup4>=4.12.0
pandas>=2.0.0
openpyxl>=3.1.0
instructor>=1.2.0
openai>=1.30.0
pydantic>=2.0.0
requests>=2.31.0
tqdm>=4.65.0
```

#### Environment Configuration
```bash
export OPENAI_API_KEY="your-api-key-here"
```

### Usage Patterns

#### Production Integration
```python
from experimental_llm_classifier import ExperimentalLLMClassifier

classifier = ExperimentalLLMClassifier(
    openai_api_key=api_key,
    prompt_variant="default",
    model="gpt-4o-mini"
)

result = await classifier.classify_single_event_detailed(
    title=event_title,
    content=event_content,
    description=event_description
)

should_keep = (
    result.is_cybersecurity_event and
    result.is_australian_relevant and
    result.overall_confidence >= 0.5
)
```

#### Batch Optimization Testing
```python
# Run comprehensive optimization
python run_filter_optimization.py

# Quick validation
python quick_filter_test.py

# Performance summary
python filter_tuning_summary.py
```

### Monitoring and Maintenance

#### Performance Tracking
- **Daily Metrics**: Monitor precision/recall trends
- **False Positive Review**: Weekly analysis of incorrectly kept events
- **False Negative Investigation**: Monthly review of missed incidents

#### Prompt Evolution
- **Pattern Updates**: Quarterly review of new rejection patterns
- **Training Data Updates**: Semi-annual training set expansion
- **A/B Testing**: Continuous testing of prompt improvements

#### Error Analysis
- **Classification Errors**: Log and analyze LLM failures
- **Confidence Calibration**: Monitor confidence score accuracy
- **Content Quality**: Track web scraping success rates

## Advanced Optimization Techniques

### Adaptive Thresholding

**Dynamic Confidence Adjustment**:
- Source-based threshold modification
- Historical performance weighting
- Content length considerations

### Multi-Stage Filtering

**Layered Approach**:
1. **Pre-filtering**: Keyword-based rejection for obvious non-incidents
2. **LLM Classification**: Full prompt-based analysis
3. **Post-filtering**: Confidence-based final decisions

### Prompt Chain Optimization

**Sequential Prompting**:
- Initial classification with lenient prompt
- Detailed analysis for borderline cases
- Final validation with strict criteria

## Troubleshooting Guide

### Common Issues

#### Low Precision (Too Many False Positives)
- **Solution**: Increase confidence threshold or use strict prompt
- **Investigation**: Review false positive patterns for new rejection rules

#### Low Recall (Too Many False Negatives)
- **Solution**: Decrease confidence threshold or use lenient prompt
- **Investigation**: Analyze missed incidents for prompt improvements

#### Inconsistent Results
- **Solution**: Check API connectivity and model availability
- **Investigation**: Review confidence score distributions

### Performance Degradation

#### Monitoring Alerts
- **Accuracy Drop**: Below 75% accuracy for 3+ days
- **Confidence Drift**: Mean confidence change >0.1 week-over-week
- **Error Rate Increase**: >5% LLM API errors

#### Recovery Procedures
1. **Validate Training Data**: Ensure training set still representative
2. **Test Prompt Variants**: Re-run optimization with current data
3. **Update Rejection Rules**: Add new patterns discovered in false positives
4. **Model Upgrades**: Test newer OpenAI models for improved performance

## Future Enhancements

### Planned Improvements
- **Multi-Model Ensemble**: Combine multiple LLM responses
- **Active Learning**: Continuous training data collection
- **Real-Time Adaptation**: Dynamic prompt adjustment based on performance
- **Domain-Specific Tuning**: Specialized prompts for different cyber event types