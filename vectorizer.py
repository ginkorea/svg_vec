#| default_exp vectorizer

#| export
import json, re, shutil, subprocess, os
from dataclasses import dataclass
from defusedxml import ElementTree as eTree
from svg_constraints import SVGConstraints
from PIL import Image


@dataclass
class SVGVectorizer:
    """SVG Vectorizer using ./bin/autoheal on ./bin/input.png"""

    def __init__(self, bin_dir: str = "./bin", img_dir: str = "./img"):
        self.constraints = SVGConstraints()
        self.bin_dir = bin_dir
        self.img_dir = img_dir
        self.layers_dir = f"{self.bin_dir}/layers"
        self.input_dir = f"{self.img_dir}/in"
        self.output_dir = f"{self.img_dir}/out"
        self.default_image = "input.png"
        self.default_output = "output.svg"
        os.makedirs(self.input_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.layers_dir, exist_ok=True)

    def autoheal(self) -> str:
        return f"{self.bin_dir}/autoheal"

    def potrace(self) -> str:
        return f"{self.bin_dir}/potrace"

    def mkbitmap(self) -> str:
        return f"{self.bin_dir}/mkbitmap"

    def vectorize(self, image_name: str = None, output_path: str = None) -> str:
        if image_name is None:
            image_name = self.default_image

        input_path = f"{self.input_dir}/{image_name}"
        if output_path is None:
            output_path = f"{self.output_dir}/{os.path.splitext(image_name)[0]}.svg"

        # Step 1: Copy image to ./bin/input.png
        bin_input_path = f"{self.bin_dir}/input.png"
        shutil.copy(input_path, bin_input_path)

        # Step 2: Run autoheal (no args)
        print(f"🚀 [autoheal] cd {self.bin_dir} && ./autoheal")
        self.run_cmd(["./autoheal"], cwd=self.bin_dir)
        # move output layers to ./bin/layers from ./bin
        self.move_layers()
        # Step 2.5: Convert layers to PBM
        self.convert_layers_to_pbm(blur_radius=10.0)

        # Step 3: Read palette.json
        palette = self.read_palette()

        # Step 4: Vectorize each layer
        layers_combined = []
        for layer_key, rgb in palette.items():
            r, g, b = rgb
            layer_filename = f"{layer_key}_r{r}_g{g}_b{b}.png"
            layer_path = f"{self.bin_dir}/layers/{layer_filename}"
            if not os.path.exists(layer_path):
                raise FileNotFoundError(f"❌ Missing layer image: {layer_filename}")

            color = f"rgb({r},{g},{b})"
            svg_snippet = self.vectorize_layer(layer_filename, color, layer_key)
            layers_combined.append(svg_snippet)

        # Step 5: Merge and write SVG
        final_svg = self._merge_layers(layers_combined)
        try:
            self.constraints.validate_svg(final_svg)
        except ValueError as e:
            print(f"❌ SVG validation failed: {e}")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_svg)

        print(f"✅ SVG saved to: {output_path}")
        return output_path

    @staticmethod
    def run_cmd(cmd, cwd=None, check=True) -> str:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"Command failed: {' '.join(cmd)}\n"
                f"Return code: {result.returncode}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def move_layers(self):
        """Move black/white PNG layers to ./bin/layers."""
        for file in os.listdir(self.bin_dir):
            if file.endswith(".png") and file != "input.png":
                src = self.bin_dir + "/" + file
                dst = self.layers_dir + "/" + file
                shutil.move(src, dst)

    def convert_layers_to_pbm(self, blur_radius=10.0, scale=1):
        """
        Convert all PNG mask layers in ./bin/layers to smoothed PBM using mkbitmap.
        Converts PNG to PGM first, then runs mkbitmap.
        """
        mkbitmap_path = self.mkbitmap()

        if not os.path.isfile(mkbitmap_path):
            raise FileNotFoundError(f"❌ mkbitmap not found at: {mkbitmap_path}")
        if not os.access(mkbitmap_path, os.X_OK):
            raise PermissionError(f"❌ mkbitmap is not executable: {mkbitmap_path}")

        for file in os.listdir(self.layers_dir):
            if file.endswith(".png"):
                png_path = os.path.join(self.layers_dir, file)
                pgm_path = png_path.replace(".png", ".pgm")
                pbm_path = png_path.replace(".png", ".pbm")

                # ✅ Step 1: Convert PNG to PGM
                print(f"🖼️ Converting {file} to PGM...")
                with Image.open(png_path) as img:
                    img.convert("L").save(pgm_path, format="PPM")

                # ✅ Step 2: Run mkbitmap on PGM to produce PBM
                cmd = [
                    mkbitmap_path,
                    "-f", str(blur_radius),
                    "-s", str(scale),
                    #"-t", str(threshold),
                    "-o", pbm_path,
                    pgm_path
                ]

                print("🔧 Running:", " ".join(cmd))
                subprocess.run(cmd, check=True)

    def read_palette(self) -> dict:
        palette_path = f"{self.bin_dir}/palette.json"
        if not os.path.exists(palette_path):
            raise FileNotFoundError("❌ palette.json not found after autoheal")

        with open(palette_path, "r", encoding="utf-8") as f:
            palette = json.load(f)

        return palette

    def vectorize_layer(self, layer_filename: str, color: str, layer_id: str) -> str:
        """
        Runs Potrace on the PBM with unit=1 (no cropping), grabs its <g> transform,
        strips any old fill/stroke, injects our fill + even-odd rule, and returns that <g>…</g>.
        """
        stem = os.path.splitext(layer_filename)[0]
        pbm_path = os.path.join(self.layers_dir, f"{stem}.pbm")
        svg_path = os.path.join(self.layers_dir, f"{stem}.svg")
        out_svg = os.path.join(self.output_dir, f"{layer_id}.svg")

        if not os.path.exists(pbm_path):
            raise FileNotFoundError(f"❌ PBM not found: {pbm_path}")

        # 1) Run Potrace WITHOUT --tight, but with --unit=1 so viewBox == PBM size
        self.run_cmd([
            self.potrace(),
            "--unit", "1",  # 1px = 1 user‐unit => width/height == 1024
            "--flat",  # straight segments only
            "--turdsize", "32",  # drop small specks
            "-a", "2.5",  # smoothing
            "-s",  # SVG output
            "-o", svg_path,
            pbm_path
        ])

        # 2) Read & save raw for inspection
        svg_text = open(svg_path, "r", encoding="utf-8").read()
        with open(out_svg, "w", encoding="utf-8") as f:
            f.write(svg_text)

        # 3) Parse the generated SVG, grab its single <g> (with translate/scale)
        tree = eTree.parse(svg_path)
        root = tree.getroot()
        ns = {"svg": "http://www.w3.org/2000/svg"}
        g = root.find("svg:g", ns)
        if g is None:
            raise ValueError(f"❌ No <g> in {svg_path}")

        transform = g.attrib.get("transform", "")

        # 4) Build our cleaned <g>—strip any old fill/stroke, apply ours + even-odd
        lines = [f'<g id="{layer_id}" transform="{transform}" fill="{color}" fill-rule="evenodd" stroke="none">']
        for p in g.findall("svg:path", ns):
            d = p.attrib.get("d", "").strip()
            lines.append(f'  <path d="{d}"/>')
        lines.append("</g>")

        return "\n".join(lines)

    @staticmethod
    def _merge_layers(layer_groups: list[str]) -> str:
        """
        Wrap all of the per-layer <g> blocks into one 1024×1024 SVG.
        Each layer keeps its own transform, so no extra math is needed here.
        """
        body = "\n".join(layer_groups)
        return f'''<svg xmlns="http://www.w3.org/2000/svg"
    width="1024" height="1024" viewBox="0 0 1024 1024">
{body}
</svg>'''

