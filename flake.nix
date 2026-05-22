{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
    in
    {
      devShells.x86_64-linux.default = pkgs.mkShell.override {
        stdenv = pkgs.clangStdenv;
      } {
        buildInputs = with pkgs; [
          just
          nodejs_22
          uv
          python3
        ];
        LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath (with pkgs; [
          libxkbcommon
          libX11
          libxcb
          libXcursor
          libXext
          libXi
          libXrandr
          libXinerama
          libxkbcommon
          libuuid
        ]);
      };
    };
}
