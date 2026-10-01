# seine

```{rubric} Debian system composition for embedded Linux
```

seine builds bootable embedded Linux images from YAML specifications. It
composes Debian packages, local changes, configuration, and kernels into a
target system while retaining the inputs needed to understand and rebuild it.

```{toctree}
:maxdepth: 2
:caption: User guide

Introduction <introduction>
getting-started
specification
merging
kernels
building
distributed-build
environment
testing
tui
ai
vault-openbao
storage-garage
release-as-debs
```

```{toctree}
:maxdepth: 2
:caption: Reference

caching
worker-setup
```

## Project resources

- [Source code](https://github.com/chombourger/seine)
- [Issue tracker](https://github.com/chombourger/seine/issues)
- [Examples](https://github.com/chombourger/seine/tree/master/examples)
