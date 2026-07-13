{
  description = "ppg3 dev shell and test matrix";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/26.05";
    naersk.url = "github:nmattia/naersk";
    naersk.inputs.nixpkgs.follows = "nixpkgs";
    rust-overlay.url = "github:oxalica/rust-overlay";
    rust-overlay.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      naersk,
      rust-overlay,
    }:
    let
      system = "x86_64-linux";
      overlays = [ (import rust-overlay) ];
      pkgs = import nixpkgs { inherit system overlays; };

      rust = pkgs.rust-bin.stable."1.93.1".default.override {
        targets = [ "x86_64-unknown-linux-gnu" ];
        extensions = [
          "llvm-tools-preview"
          "clippy"
          "rust-src"
        ];
      };

      naersk-lib = naersk.lib.${system}.override {
        cargo = rust;
        rustc = rust;
      };

      # The pyo3 extension (`ppg3-py` crate) is built once against pyo3's
      # stable ABI (abi3-py39, see py/Cargo.toml), so a single build serves
      # every interpreter in the pytest matrix below. naersk copies the
      # cdylib (`libppg3_core_ext.so`) into $out/lib; the pytest check
      # renames it to the `_core.abi3.so` name Python's import machinery
      # looks for inside the `ppg3` package.
      ppg3-ext = naersk-lib.buildPackage {
        pname = "ppg3-core-ext";
        version = "0.1.0";
        src = ./.;
        copyLibs = true;
        copyBins = false;
        cargoBuildOptions =
          x:
          x
          ++ [
            "-p"
            "ppg3-py"
            "--lib"
          ];
      };

      # `cargo test` over the pure-Rust half of the workspace (core + cli).
      # `ppg3-py` is excluded: it's an extension module, exercised end-to-end
      # by the pytest checks instead. doCheck makes naersk run the tests as
      # part of building this derivation.
      ppg3-cargo-tests = naersk-lib.buildPackage {
        pname = "ppg3-cargo-tests";
        version = "0.1.0";
        src = ./.;
        doCheck = true;
        # Install the `ppg3` CLI binary so the derivation has a non-empty
        # output; the real point of this check is `doCheck` (cargo test).
        copyLibs = false;
        copyBins = true;
        # A handful of core tests shell out to `which` / `true` (the nix build
        # sandbox has neither on PATH by default). `/bin/sh` is provided by
        # nix itself, so the many `/bin/sh`-based tests already work.
        nativeBuildInputs = [
          pkgs.which
          pkgs.coreutils
        ];
        cargoBuildOptions =
          x:
          x
          ++ [
            "-p"
            "ppg3-core"
            "-p"
            "ppg3-cli"
          ];
        cargoTestOptions =
          x:
          x
          ++ [
            "-p"
            "ppg3-core"
            "-p"
            "ppg3-cli"
            # `none_executor_scrubs_env_to_declared_set_plus_defaults` spawns
            # the hardcoded absolute path `/usr/bin/env`, which exists on a
            # NixOS host but not inside nix's hermetic build sandbox. Skip it
            # here; it still runs in `nix develop` / on the host.
            "--"
            "--skip"
            "none_executor_scrubs_env_to_declared_set_plus_defaults"
          ];
      };

      # Python versions covered by the pytest matrix. All three ship working
      # blake3 / libcst / cloudpickle in nixpkgs 26.05.
      pythonTestVersions = [
        "312"
        "313"
        "314"
      ];

      # `ppg3` assembled as a real installed package: the pure-python sources
      # under python/ppg3 plus the prebuilt abi3 extension renamed to the
      # `_core.abi3.so` import name. Installing it into the interpreter's
      # site-packages (rather than only prepending python/ to sys.path) is
      # what lets ppg3's forkserver/watch children — clean subprocess pythons
      # with a scrubbed environment — import `ppg3` and `ppg3._core` at all.
      mkPpg3Pkg =
        python:
        python.pkgs.buildPythonPackage {
          pname = "ppg3";
          version = "0.1.0";
          format = "other";
          src = ./python;
          dontConfigure = true;
          dontBuild = true;
          doCheck = false;
          installPhase = ''
            runHook preInstall
            site=$out/${python.sitePackages}
            mkdir -p $site
            cp -r ppg3 $site/ppg3
            cp ${ppg3-ext}/lib/libppg3_core_ext.so $site/ppg3/_core.abi3.so
            runHook postInstall
          '';
        };

      # Third-party python deps (pyproject.toml's `test` group). Shared by the
      # pytest checks and the dev shell.
      pyDeps = ps: [
        ps.pytest
        ps.cloudpickle
        ps.blake3
        ps.libcst
      ];

      # Sealed test env: third-party deps + a *built* copy of ppg3 in
      # site-packages. Used by the hermetic pytest checks.
      mkTestVenv =
        ver:
        let
          python = pkgs.${"python" + ver};
        in
        python.withPackages (ps: pyDeps ps ++ [ (mkPpg3Pkg python) ]);

      # Dev env: third-party deps only, NO ppg3 — the dev shell puts the live
      # working tree on PYTHONPATH instead (see the shellHook), so edits to
      # python/ppg3 take effect without a rebuild.
      mkDevEnv = ver: pkgs.${"python" + ver}.withPackages pyDeps;

      mkPytestCheck =
        ver:
        let
          venv = mkTestVenv ver;
        in
        pkgs.runCommand "pytest-python${ver}"
          {
            nativeBuildInputs = [ venv ];
          }
          ''
            mkdir -p work
            cp -r ${./python} work/python
            cp -r ${./tests} work/tests   # conftest resolves ../tests/golden
            chmod -R u+w work
            cp ${ppg3-ext}/lib/libppg3_core_ext.so work/python/ppg3/_core.abi3.so
            cd work/python
            export HOME=$TMPDIR
            pytest tests
            touch $out
          '';

      pytestChecks = builtins.listToAttrs (
        map (ver: {
          name = "pytest-python${ver}";
          value = mkPytestCheck ver;
        }) pythonTestVersions
      );

    in
    {
      checks.${system} = pytestChecks // {
        cargo-tests = ppg3-cargo-tests;
      };

      devShells.${system}.default = pkgs.mkShell {
        # Pin the rust target dir so rust-analyzer and manual cargo runs don't
        # clobber each other / trigger constant pyo3 rebuilds.
        CARGO_TARGET_DIR = "target_rust_analyzer";
        nativeBuildInputs = [
          rust
          pkgs.cargo-binutils
          pkgs.rust-analyzer
          pkgs.bacon
          pkgs.maturin
          pkgs.git
          (mkDevEnv "313")
        ];

        # Editable ppg3: rather than installing a frozen copy, put the live
        # working tree on PYTHONPATH so python/ppg3 edits are picked up
        # immediately. ppg3's forkserver forwards PYTHONPATH to its template
        # children (core/src/forkserver.rs `template_spawn_env`), so those
        # subprocesses import the same live tree. The prebuilt abi3 extension
        # is symlinked in so `import ppg3._core` works out of the box; re-run
        # `maturin develop -m python/pyproject.toml` (or `cargo build -p
        # ppg3-py`) after touching the Rust half. Delete the symlink and it
        # will be recreated on the next `nix develop`.
        shellHook = ''
          repo_root=$(${pkgs.git}/bin/git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
          export PYTHONPATH="$repo_root/python''${PYTHONPATH:+:$PYTHONPATH}"
          so="$repo_root/python/ppg3/_core.abi3.so"
          if [ ! -e "$so" ]; then
            ln -s ${ppg3-ext}/lib/libppg3_core_ext.so "$so"
            echo "ppg3: linked prebuilt _core.abi3.so (run 'maturin develop -m python/pyproject.toml' to build your own)"
          fi
        '';
      };

      # Keep the pre-flakes `nix develop`-less workflow working too.
      devShell.${system} = self.devShells.${system}.default;
    };
}
