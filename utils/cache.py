"""
Performance caching utility for analytics queries
Usage:
    from utils.cache import cached
    
    @cached(ttl_seconds=300)
    def expensive_function():
        ...
"""
from functools import wraps
from datetime import datetime, timedelta
from typing import Any, Callable

# Simple in-memory cache store
_cache_store = {}

def cached(ttl_seconds: int = 300):
    """
    Cache decorator with TTL (time-to-live)
    
    Args:
        ttl_seconds: Time in seconds before cache expires (default: 5 minutes)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # Create cache key from function name and arguments
            cache_key = f"{func.__module__}.{func.__name__}:{str(args)}:{str(sorted(kwargs.items()))}"
            
            # Check if cached and not expired
            if cache_key in _cache_store:
                cached_data, timestamp = _cache_store[cache_key]
                age = datetime.now() - timestamp
                
                if age < timedelta(seconds=ttl_seconds):
                    # Cache hit - return cached data
                    return cached_data
            
            # Cache miss or expired - call function
            result = func(*args, **kwargs)
            
            # Store in cache
            _cache_store[cache_key] = (result, datetime.now())
            
            return result
        
        return wrapper
    return decorator

def invalidate_cache():
    """Clear all cached data"""
    global _cache_store
    _cache_store = {}
    
def get_cache_stats():
    """Get cache statistics"""
    return {
        "entries": len(_cache_store),
        "keys": list(_cache_store.keys())
    }
