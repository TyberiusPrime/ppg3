{
  description = "ppg3 dev shell and test matrix";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/26.05";
    naersk.url = "github:nmattia/naersk";
    naersk.inputs.nixpkgs.follows = "nixpkgs";
    rust-overlay.url = "github:oxalica/rust-overlay";
    rust-overlay.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      rust-overlay,
      naersk,
      ...
    }:
    let
      overlays = [ (import rust-overlay) ];

      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };
      pythonSets = forAllSystems (
        system:
        let
          overlays = [ (import rust-overlay) ];
          _pkgs = import nixpkgs { inherit system overlays; };
          rust = _pkgs.rust-bin.stable."1.93.1".default.override {
            targets = [
              "x86_64-unknown-linux-gnu"
              "x86_64-unknown-linux-musl"
            ];
            extensions = [ "llvm-tools-preview" ];
          };
          pkgs_with_rust = _pkgs // {
            cargo = rust;
            rustc = rust;
            cargo-binutils = pkgs.cargo-binutils.override {
              cargo = rust;
              rustc = rust;
            };
          };

          pkgs = pkgs_with_rust;
          python = pkgs.python3;
          hacks = pkgs.callPackage pyproject-nix.build.hacks { };

        in
        (pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        }).overrideScope
          (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              overlay
              (final: prev: {
                ppg3 =
                  (hacks.importCargoLock {
                    prev = prev.ppg3;
                  }).overrideAttrs
                    (old: {
                      src = lib.fileset.toSource {
                        root = ./.;
                        fileset = lib.fileset.unions [
                          ./python/ppg3
                          ./crates
                          ./Cargo.toml
                          ./Cargo.lock
                          ./pyproject.toml
                        ];
                      };
                      MATURIN_NO_INSTALL_RUST = 1;
                      buildInputs = old.buildInputs or [ ] ++ [
                        pkgs.cargo
                        pkgs.rustc
                      ];
                      cargoDeps = pkgs.rustPlatform.importCargoLock {
                        lockFile = ./Cargo.lock;
                      };

                    });
              })
            ]
          )
      );

    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system overlays; };
          pythonSet = pythonSets.${system}.overrideScope editableOverlay;
          virtualenv = pythonSet.mkVirtualEnv "ppg3-env" workspace.deps.all;
          naersk-lib = naersk.lib.${system}.override {
            cargo = pkgs.cargo;
            rustc = pkgs.rustc;
          };

          ppg3-cli = naersk-lib.buildPackage {
            pname = "ppg3-cli";
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
                "ppg3-cli"
              ];
            cargoTestOptions =
              x:
              x
              ++ [
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
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
              pkgs.rustc
              pkgs.cargo
              pyproject-nix.packages.${system}.build-editable
              ppg3-cli
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)

              export MATURIN_NO_INSTALL_RUST=1
              # Re-run editable package build for side effects
              build-editable
            '';
          };
        }
      );

      packages = forAllSystems (system: {
        default = pythonSets.${system}.mkVirtualEnv "ppg3-env" workspace.deps.default;
      });
    };
}
