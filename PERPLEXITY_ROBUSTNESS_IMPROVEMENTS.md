# Perplexity Data Source Robustness Improvements

## Overview
Enhanced the Perplexity data source to be robust against various types of API failures, including the specific 401 Authorization errors that were causing pipeline failures.

## Problem Addressed
**Original Error:**
```
2025-09-28 15:37:04,926 - ERROR - Perplexity search failed for query 'Australian cyber attack after:05/01/2020 before:05/31/2020 data breach security incident': <html>
<head><title>401 Authorization Required</title></head>
<body>
<center><h1>401 Authorization Required</h1></center>
...
```

The script was failing completely when Perplexity API returned authentication errors, with no retry logic or graceful degradation.

## Improvements Implemented

### 1. Comprehensive Error Classification
Added intelligent error detection for different types of failures:

#### Authentication Errors (401/403)
- **Detection**: `_is_auth_error()` method
- **Handling**: No retries (permanent failure), log helpful message about API key
- **Examples**: 401 Authorization, 403 Forbidden, Invalid API key

#### Rate Limiting Errors (429)
- **Detection**: `_is_rate_limit_error()` method
- **Handling**: Retry with extended delays, automatic backoff
- **Examples**: 429 Too Many Requests, Rate limit exceeded

#### Server Errors (5xx)
- **Detection**: `_is_server_error()` method
- **Handling**: Retry with exponential backoff
- **Examples**: 500 Internal Server Error, 502 Bad Gateway, 503 Service Unavailable

#### Network Errors
- **Detection**: `_is_network_error()` method
- **Handling**: Retry with backoff
- **Examples**: Connection timeout, DNS resolution failed

### 2. Exponential Backoff with Jitter
```python
delay = min(base_delay * (backoff_multiplier ** (attempt - 1)), max_delay)
jitter = delay * 0.1 * (0.5 - asyncio.get_event_loop().time() % 1)
total_delay = delay + jitter
```

**Configuration:**
- Base delay: 2.0 seconds
- Max delay: 60.0 seconds
- Backoff multiplier: 2.0
- Max retries: 3

### 3. Circuit Breaker Pattern
Prevents overwhelming the API during extended outages:

- **Threshold**: 5 consecutive failures
- **Backoff period**: 5 minutes
- **Reset**: Automatic after successful request

### 4. Intelligent Retry Logic
```python
async def _search_with_retry(self, query: str, date_range: DateRange) -> PerplexitySearchResults:
    for attempt in range(self.max_retries + 1):
        try:
            return await self._search(query, date_range)
        except Exception as exc:
            if self._is_auth_error(exc):
                # Don't retry auth errors - they won't resolve
                raise exc
            elif self._is_rate_limit_error(exc):
                # Retry with longer delay
                pass
            # ... handle other error types
```

### 5. Enhanced Logging
Different log levels for different error types:

- **ERROR**: Authentication failures, permanent errors
- **WARNING**: Rate limits, temporary server issues
- **INFO**: Retry attempts, recovery actions
- **DEBUG**: Individual query processing

### 6. Graceful Degradation
- Continue processing other queries even if some fail
- Return partial results rather than complete failure
- Provide summary statistics of success/failure rates

## New Behavior

### Before
```
ERROR - Perplexity search failed for query '...': 401 Authorization Required
[Pipeline stops completely]
```

### After
```
ERROR - Perplexity authentication failed for query '...': 401 Authorization Required
ERROR - Please check your PERPLEXITY_API_KEY configuration
INFO - Perplexity collection completed: 4 successful, 3 failed queries
[Pipeline continues with other data sources]
```

## Configuration Options
The robustness can be tuned via class attributes:

```python
self.max_retries = 3                    # Number of retry attempts
self.base_delay = 2.0                   # Initial delay in seconds
self.max_delay = 60.0                   # Maximum delay in seconds
self.backoff_multiplier = 2.0           # Exponential backoff factor
self.circuit_breaker_threshold = 5      # Failures before circuit breaker
```

## Error Handling Matrix

| Error Type | Detection | Retry? | Delay | Log Level | Action |
|------------|-----------|--------|-------|-----------|---------|
| 401/403 Auth | `_is_auth_error()` | No | - | ERROR | Stop, check API key |
| 429 Rate Limit | `_is_rate_limit_error()` | Yes | Extended | WARNING | Backoff 30s + exponential |
| 5xx Server | `_is_server_error()` | Yes | Exponential | WARNING | Standard retry |
| Network | `_is_network_error()` | Yes | Exponential | WARNING | Standard retry |
| Unknown | Default | Yes | Exponential | ERROR | Standard retry |

## Testing
Comprehensive test suite verifies:
- ✅ Error classification accuracy
- ✅ Circuit breaker functionality
- ✅ Retry configuration
- ✅ Exponential backoff calculation
- ✅ Success/failure tracking

## Benefits

### Reliability
- **Before**: Single API failure stops entire pipeline
- **After**: Graceful handling of temporary failures, continues with other queries/sources

### Observability
- **Before**: Generic error messages
- **After**: Specific error types with actionable guidance

### Performance
- **Before**: No backoff, potential API abuse
- **After**: Intelligent delays prevent overwhelming APIs

### Maintenance
- **Before**: Manual intervention required for API issues
- **After**: Automatic recovery from temporary issues

## Usage Examples

### Successful Query with Retries
```
DEBUG - Processing Perplexity query 1/7: Australian cyber attack after:05/01/2020...
WARNING - Server error on attempt 1, will retry: 503 Service Unavailable
INFO - Retrying Perplexity query in 2.1s (attempt 2/4)
DEBUG - Successfully processed query 1, found 3 events
```

### Authentication Error (No Retry)
```
ERROR - Authentication error on attempt 1, not retrying: 401 Authorization Required
ERROR - Please check your PERPLEXITY_API_KEY configuration
```

### Circuit Breaker Activation
```
WARNING - Skipping Perplexity collection due to circuit breaker (too many recent failures)
```

## Future Enhancements
- **Adaptive retry delays** based on API response headers
- **Query prioritization** during rate limiting
- **Cached fallbacks** for repeated queries
- **Health check endpoints** for proactive monitoring

---

**Status**: ✅ Implemented and Tested
**Version**: 1.0
**Date**: 2024-09-28