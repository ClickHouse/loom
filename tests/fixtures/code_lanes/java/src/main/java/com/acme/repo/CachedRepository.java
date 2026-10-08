package com.acme.repo;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Component;

/** Synthetic generic class: an annotation, an implemented interface, a
 *  constructor and a delegating call. */
@Component
public class CachedRepository<T, ID> implements Repository<T, ID> {
    private final Repository<T, ID> inner;
    private final Map<ID, T> cache = new HashMap<>();

    public CachedRepository(Repository<T, ID> inner) {
        this.inner = inner;
    }

    @Override
    public List<T> all() {
        return inner.all();
    }

    @Override
    public T byId(ID id) {
        return cache.computeIfAbsent(id, inner::byId);
    }
}
