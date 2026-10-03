# eLxr comparison and ideas for seine

_Research note, 2026-10-02.  “eLxr” here means the public eLxr
Debian-derived edge-to-cloud distribution, not an unrelated project with a
similar name._

## Bottom line

eLxr and seine occupy adjacent layers rather than being like-for-like build
systems.  eLxr is a curated, released Debian derivative: it owns package
components, support/CVE policy, published images, and a hardware
qualification programme.  Its relevant image-build tool is
[Rucksack](https://gitlab.com/elxr/tools/rucksack), which builds YAML-defined
Debian images.  seine is an image-composition tool: a project chooses its
Debian feeds, packages, local grafts, configuration and tests, then produces
a reproducible target image without creating another package universe.

That makes eLxr a plausible *input distribution* for a seine specification
(a feed plus its archive key and components), or a reference for release
operations.  Reimplementing eLxr’s distribution machinery in seine would
blur seine’s deliberately narrower boundary.

## Evidence-based comparison

| Concern | eLxr | seine today | Implication |
| --- | --- | --- | --- |
| Product boundary | An open-source Debian-based Linux distribution for edge-to-cloud use; it curates and hardens packages. [eLxr docs](https://docs.elxr.org/aria/index.html), [project README](https://gitlab.com/elxr/gitlab-profile/-/blob/main/README.md) | Composes an image from ordinary Debian packages, optional Debian-package grafts and Ansible; it explicitly does not create a new distribution or package format. [README](../README.md) | Complementary: make eLxr easy to consume, rather than competing as a distro. |
| Package policy | Its archive separates supported `main` from community-derived components whose security-fix commitment differs. [Package components](https://elxr.org/package-components) | A specification already models arbitrary APT feeds, components and `signed-by` trust, while a committed vendor lock records versions and hashes. [Specification](specification.md#feeds), [README](../README.md) | A feed/component policy profile and a CI guard against mixing support tiers would be useful—not a new repository implementation. |
| Image construction | “Distro-to-order” is YAML-defined and builds an eLxr raw image in a rucksack container; it has an OSTree variant. [D2O guide](https://docs.elxr.org/aria/elxr-distro-to-order-development-guide/index.html), [Rucksack README](https://gitlab.com/elxr/tools/rucksack/-/blob/b67912a458f51416879be7af02f5fe80032e623b/README.md#L5-20) | YAML specifications are composable, build root filesystems in rootless Podman, then create disk images through libguestfs/QEMU. [README](../README.md), [Merging](merging.md) | Both value declarative image definitions. seine’s composability and explicit input locks are a stronger fit for project-local variations. |
| Build-engine model | Rucksack runs a linear list of named `stages.install` plugins. [executor](https://gitlab.com/elxr/tools/rucksack/-/blob/b67912a458f51416879be7af02f5fe80032e623b/rucksack/build.py#L24-60) Its example pipeline cleanly names bootstrap, image, partition, filesystem, bootloader and compression steps, but also uses imperative hooks and a fixed loop device. [example](https://gitlab.com/elxr/tools/rucksack/-/blob/b67912a458f51416879be7af02f5fe80032e623b/rucksack/tests/configs/minimal/image.yaml#L11-92) | seine derives a planned, cached build from declarative sections and keeps the inputs necessary to rebuild it. [README](../README.md), [Caching](caching.md) | Borrow the understandable stage vocabulary for plans/UI; do **not** replace seine’s input/digest model with a generic imperative plugin executor. |
| Lifecycle updates | Current Edge images use OSTree for live system and user-space updates; eLxr also publishes a release lifecycle. [OSTree overview](https://docs.elxr.org/bianca/elxr-quick-start/elxr-overview.html), [release lifecycle](https://docs.elxr.org/elxr-releases.html) | Build-time reproducibility, vendoring and signing are covered; the declared scope ends at producing and validating images. [README](../README.md) | The most consequential gap to evaluate is an *optional deployment/update* layer: OSTree/bootc-style atomic update and rollback outputs, with a separate signed-update-repository workflow. |
| Hardware confidence | eLxr maintains LAVA job templates and a versioned verification portal for hardware/software results. [LAVA templates](https://gitlab.com/elxr/test/lava/lava-job-template), [verification portal](https://gitlab.com/elxr/website/elxr-verified-portal/-/tree/main) | Tests are part of a specification and run against real targets via mtda/Robot Framework; results include Robot artifacts. [Testing](testing.md) | Add a small, versioned compatibility-result schema/export and a publishable static report. Keep mtda/Robot as the execution engine; do not replace it with LAVA. |
| Release/build operations | eLxr is organized as many distribution, image, CVE and verification projects. [project inventory](https://elxr.org/projects) | seine has build-result SBOMs, OpenBao-backed signing and a distributed worker system with capability-aware scheduling and S3 artifact manifests. [README](../README.md), [Distributed builds](distributed-build.md) | seine is already ahead in project-local, reproducible build orchestration; operational polish should focus on release promotion and evidence, not another build farm. |

## Things worth considering, in priority order

1. **eLxr feed profile/example.** Add a maintained example (and perhaps a
   named profile) showing `aria`/`bianca`, archive key, component selection,
   and the support-tier warning.  This is low-risk and proves that seine can
   consume an eLxr derivative without special cases.
2. **Atomic update deliverable as an optional extension.** Investigate whether
   a seine image can emit an OSTree commit/static-delta or bootc-compatible
   artifact from exactly the locked rootfs inputs it already builds.  Require
   update metadata to include the spec digest, vendor-lock digest, SBOM and
   signing identity.  This is useful only if deployed devices need rollback
   and high-uptime updates; it should not become the default image path.
3. **Qualification evidence.** Define a portable JSON/YAML record keyed by
   `(spec digest, board, firmware, kernel, test suite, result, timestamp)`;
   have `seine test` emit it and render a static compatibility matrix.  This
   adapts eLxr’s useful “what was verified against which release?” discipline
   to seine’s project-specific tests.
4. **Release promotion contract.** Build on the existing S3 artifact manifest:
   explicitly promote an immutable build ID from development to production
   only after test evidence, SBOM/security scan, signature verification and
   approval are present.  This is a release workflow feature, not a package
   repository feature.
5. **Curated profile catalogue and target matrix.** eLxr keeps reusable
   package fragments separate from target/image manifests and maintains a
   broad VM, board and cloud catalogue. [eLxr config README](https://gitlab.com/elxr/tools/elxr-config/-/blob/582c6e5b754dfe1724c2f4e7d99ad2d8c4d5dbce/README.md#L42-55)
   Package a small, separately versioned collection of seine `requires:`
   fragments plus CI-built reference targets (for example amd64 VM, arm64
   board and one cloud/container target).  This builds on seine’s existing
   controlled merging rather than importing eLxr’s tooling.
6. **Cache-proxy setting, if measurements show cold APT downloads dominate.**
   Rucksack has an explicit APT cache-proxy option. [README](https://gitlab.com/elxr/tools/rucksack/-/blob/b67912a458f51416879be7af02f5fe80032e623b/README.md#L107-109)
   A corresponding seine setting should remain only a transport optimisation:
   lock hashes and provenance, never proxy availability, remain authoritative.

## Explicit non-recommendations

- Do not turn seine into a Debian derivative with its own broad package
  curation, component/CVE promises or mirror fleet.  eLxr’s component policy
  is valuable context, but it needs distribution-scale ownership.
- Do not replace Debian packages and grafts with an eLxr-specific package
  mechanism.  The shared Debian base is an interoperability advantage.
- Do not adopt privileged Docker-in-Docker merely to mirror eLxr’s documented
  D2O procedure.  seine’s rootless Podman/libguestfs pipeline is materially
  better aligned with its current security and remote-worker model.
- Do not copy Rucksack’s generic linear plugin executor, permissive schema, or
  imperative hooks.  Those trade away the explicit planning, cache-key and
  reproducibility guarantees that are central to seine.

## Source notes

All external references above are first-party eLxr documentation or public
eLxr GitLab repositories, checked on 2026-10-02.  The Rucksack and eLxr-config
source links are pinned to commits `b67912a` and `582c6e5`, respectively.
Claims about seine link to the repository’s own documentation.  The
recommendations are inferences from those sources, not claims that either
project already implements them.
