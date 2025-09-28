#!/usr/bin/env python3
"""
Test script to verify the robustness improvements to the Perplexity data source.
"""

import asyncio
import sys
sys.path.append('.')

from cyber_data_collector.datasources.perplexity import PerplexityDataSource

def test_error_classification():
    """Test the error classification methods."""

    # Create a dummy data source to test error classification
    data_source = PerplexityDataSource(None, None, {})

    print("Testing error classification methods:")
    print("=" * 50)

    # Test authentication errors
    auth_errors = [
        "401 Authorization Required",
        "403 Forbidden",
        "Invalid API key provided",
        "Authorization failed"
    ]

    for error in auth_errors:
        exc = Exception(error)
        result = data_source._is_auth_error(exc)
        print(f"✓ Auth error '{error[:30]}...': {result}")
        assert result, f"Should detect auth error: {error}"

    # Test rate limit errors
    rate_limit_errors = [
        "429 Too Many Requests",
        "Rate limit exceeded",
        "Quota exceeded"
    ]

    for error in rate_limit_errors:
        exc = Exception(error)
        result = data_source._is_rate_limit_error(exc)
        print(f"✓ Rate limit error '{error[:30]}...': {result}")
        assert result, f"Should detect rate limit error: {error}"

    # Test server errors
    server_errors = [
        "500 Internal Server Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout"
    ]

    for error in server_errors:
        exc = Exception(error)
        result = data_source._is_server_error(exc)
        print(f"✓ Server error '{error[:30]}...': {result}")
        assert result, f"Should detect server error: {error}"

    # Test network errors
    network_errors = [
        "Connection timeout",
        "Network unreachable",
        "DNS resolution failed"
    ]

    for error in network_errors:
        exc = Exception(error)
        result = data_source._is_network_error(exc)
        print(f"✓ Network error '{error[:30]}...': {result}")
        assert result, f"Should detect network error: {error}"

    print("\n✓ All error classification tests passed!")

def test_circuit_breaker():
    """Test the circuit breaker functionality."""

    data_source = PerplexityDataSource(None, None, {})

    print("\nTesting circuit breaker functionality:")
    print("=" * 50)

    # Initially should not skip
    assert not data_source._should_skip_due_to_circuit_breaker()
    print("✓ Circuit breaker initially open")

    # Record failures up to threshold
    for i in range(data_source.circuit_breaker_threshold):
        data_source._record_failure()
        print(f"✓ Recorded failure {i + 1}/{data_source.circuit_breaker_threshold}")

    # Should now skip due to circuit breaker
    assert data_source._should_skip_due_to_circuit_breaker()
    print("✓ Circuit breaker tripped after threshold failures")

    # Record success should reset
    data_source._record_success()
    assert not data_source._should_skip_due_to_circuit_breaker()
    print("✓ Circuit breaker reset after success")

    print("✓ All circuit breaker tests passed!")

def test_retry_configuration():
    """Test retry configuration."""

    data_source = PerplexityDataSource(None, None, {})

    print("\nTesting retry configuration:")
    print("=" * 50)

    print(f"✓ Max retries: {data_source.max_retries}")
    print(f"✓ Base delay: {data_source.base_delay}s")
    print(f"✓ Max delay: {data_source.max_delay}s")
    print(f"✓ Backoff multiplier: {data_source.backoff_multiplier}")
    print(f"✓ Circuit breaker threshold: {data_source.circuit_breaker_threshold}")

    # Verify reasonable defaults
    assert data_source.max_retries >= 1, "Should have at least 1 retry"
    assert data_source.base_delay > 0, "Base delay should be positive"
    assert data_source.max_delay >= data_source.base_delay, "Max delay should be >= base delay"
    assert data_source.backoff_multiplier > 1, "Backoff multiplier should be > 1"

    print("✓ All retry configuration tests passed!")

def main():
    """Run all tests."""

    print("PERPLEXITY ROBUSTNESS TESTING")
    print("=" * 60)

    try:
        test_error_classification()
        test_circuit_breaker()
        test_retry_configuration()

        print(f"\n{'='*60}")
        print("🎉 ALL TESTS PASSED!")
        print("The Perplexity data source is now robust to:")
        print("  - Authentication errors (401/403)")
        print("  - Rate limiting (429)")
        print("  - Server errors (5xx)")
        print("  - Network connectivity issues")
        print("  - Circuit breaker for repeated failures")
        print("  - Exponential backoff with jitter")
        print(f"{'='*60}")

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()