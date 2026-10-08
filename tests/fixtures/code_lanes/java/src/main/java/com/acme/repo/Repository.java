package com.acme.repo;

import java.util.List;

/** Synthetic generic repository interface. */
public interface Repository<T, ID> {
    List<T> all();

    T byId(ID id);
}
