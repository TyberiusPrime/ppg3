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

      mkTestVenv =
        ver:
        let
          python = pkgs.${"python" + ver};
        in
        python.withPackages (ps: [
          ps.pytest
          ps.cloudpickle
          ps.blake3
          ps.libcst
          (mkPpg3Pkg python)
        ]);

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
          (mkTestVenv "313")
        ];
      };

      # Keep the pre-flakes `nix develop`-less workflow working too.
      devShell.${system} = self.devShells.${system}.default;
    };
}
