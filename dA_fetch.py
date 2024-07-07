from re import search
from json import loads
from io import BytesIO
from base64 import b64decode
from binascii import Error as b64decodeError
from typing import Iterator
from collections import namedtuple
from argparse import ArgumentParser, ArgumentError
from dataclasses import dataclass
from collections import deque
from urllib.parse import urlparse

from requests import get
from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageDraw, ImageColor
from png.png import Reader
from tqdm import tqdm

Slice = namedtuple("Slice", ["x", "y", "width", "height"])


@dataclass
class DAImage:
	source_url: str
	fast: bool = False
	min_chunk: int = 2

	@staticmethod
	def find_initial_state_script(tag: Tag) -> bool:
		if tag.name != "script":
			return False
		if len(tag.text) == 0:
			return False
		return "window.__INITIAL_STATE__" in tag.text

	@staticmethod
	def jwt_info(jwt: str) -> tuple[str, int, int]:
		jwt_parts = jwt.split(".")
		for padcount in range(0, 3):
			try:
				jwt_payload = b64decode(jwt_parts[1] + "=" * padcount)
				break
			except b64decodeError:
				continue
		else:
			return jwt, 0, 0
		try:
			jwt_params = loads(jwt_payload)
			max_height = int(jwt_params["obj"][0][0]["height"][2:])
			max_width = int(jwt_params["obj"][0][0]["width"][2:])
		except (ValueError, KeyError):
			return jwt, 0, 0
		return jwt, max_width, max_height

	@staticmethod
	def make_recovery_slices(tainted_slice: Slice) -> list[Slice]:
		mid_width = tainted_slice.width // 2
		mid_height = tainted_slice.height // 2
		jobs = [  # top left, top right, bottom left, bottom right
			Slice(tainted_slice.x, tainted_slice.y, mid_width, mid_height),
			Slice(tainted_slice.x + mid_width, tainted_slice.y, tainted_slice.width - mid_width, mid_height),
			Slice(tainted_slice.x, tainted_slice.y + mid_height, mid_width, tainted_slice.height - mid_height),
			Slice(tainted_slice.x + mid_width, tainted_slice.y + mid_height, tainted_slice.width - mid_width,
					tainted_slice.height - mid_height)
		]
		return jobs

	def __post_init__(self):
		da_page = get(self.source_url)
		da_html = BeautifulSoup(da_page.text, "html.parser")
		initial_state_search = da_html.find_all(self.find_initial_state_script)
		assert len(initial_state_search) > 0, "Failed to find initial state script."
		initial_state_script = initial_state_search[0].text
		initial_state_blob_match = search(r'window\.__INITIAL_STATE__ = JSON\.parse\("(.+?)"\);', initial_state_script)
		initial_state_blob_escaped = initial_state_blob_match.groups()[0]
		initial_state_blob = initial_state_blob_escaped.encode("utf-8").decode("unicode-escape")
		initial_state = loads(initial_state_blob)
		deviation_id = next(initial_state["@@entities"]["deviationExtended"].keys().__iter__())
		basic_data = initial_state["@@entities"]["deviation"][deviation_id]
		extended_data = initial_state["@@entities"]["deviationExtended"][deviation_id]

		if "download" in extended_data:
			print("This image can already be downloaded directly from dA. Exiting")
			exit(1)

		if extended_data["originalFile"]["type"] not in ["png", "jpg"]:
			print(f"Unsupported file type: {extended_data['originalFile']['type']}")
			exit(1)

		if "hasWatermark" in extended_data and extended_data["hasWatermark"]:
			print("Watermark detected, all slices will be tainted. Disabling recovery.")
			self.fast = True

		self.target_width = extended_data["originalFile"]["width"]
		self.target_height = extended_data["originalFile"]["height"]
		self.base_uri = basic_data["media"]["baseUri"]
		self.pretty_name = basic_data["media"]["prettyName"]

		if self.base_uri.endswith("jpg"):
			print("JPG mode, Can't check for compressed slices.")
			self.taint_check = False
			self.image = Image.new("YCbCr", (self.target_width, self.target_height))
		else:
			self.taint_check = True
			self.image = Image.new("RGBA", (self.target_width, self.target_height))
			self.taints = deque()

		# sometimes the JWT parameters have a larger maximum resolution
		self.jwt, self.slice_max_width, self.slice_max_height = self.jwt_info(basic_data["media"]["token"][0])
		# fall back to dA page metadata in case parsing JWT fails
		if not self.slice_max_width:
			full_view = next(filter(lambda v: v["t"] == "fullview", basic_data["media"]["types"]).__iter__())
			self.slice_max_width, self.slice_max_height = full_view["w"], full_view["h"]
		self.progress = tqdm(total=self.target_width * self.target_height, unit="px", unit_scale=True,
								bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}[{elapsed}]")

	def make_slices(self) -> Iterator[Slice]:
		for xoff in range(0, self.target_width, self.slice_max_width):
			for yoff in range(0, self.target_height, self.slice_max_height):
				if xoff + self.slice_max_width > self.target_width:
					width = self.target_width % self.slice_max_width
				else:
					width = self.slice_max_width
				if yoff + self.slice_max_height > self.target_height:
					height = self.target_height % self.slice_max_height
				else:
					height = self.slice_max_height
				yield Slice(xoff, yoff, width, height)

	def fetch_slice(self, slice: Slice) -> tuple[bool, Image]:
		slice_url = (f"{self.base_uri}/v1/crop/"
					 f"x_{slice.x},y_{slice.y},w_{slice.width},h_{slice.height},q_100/"
					 f"{self.pretty_name}-pre.png?token={self.jwt}")
		image_resp = get(slice_url)
		image_binary = image_resp.content
		if image_resp.status_code != 200:
			self.progress.write(f"Fetch failure: {slice_url}")
			return True, Image.new("RGBA", (1, 1))
		image = Image.open(BytesIO(image_binary))
		if self.taint_check:
			purepng = Reader(bytes=image_binary)
			_, _, _, info = purepng.read()
			# https://purepng.readthedocs.io/en/latest/chunk.html#phys
			# https://derpibooru.org/forums/meta/topics/userscript-semi-automated-derpibooru-uploader?post_id=5572115#post_5572115
			x_r, y_r = info["resolution"][0]
			return x_r == 1000, image
		else:
			return False, image

	def recover_section_or_skip(self, tainted_section: Slice):
		if tainted_section.width <= self.min_chunk * 2 or tainted_section.height <= self.min_chunk * 2:
			too_small = True
		else:
			too_small = False
		for subsection in self.make_recovery_slices(tainted_section):
			tainted, section = self.fetch_slice(subsection)
			if tainted and not too_small:
				self.recover_section_or_skip(subsection)
				continue
			if tainted and too_small:
				self.taints.append((subsection.x,
									subsection.y,
									subsection.x + subsection.width - 1,
									subsection.y + subsection.height - 1))

			self.progress.update(subsection.width * subsection.height)
			self.image.paste(section, (subsection.x, subsection.y))

	def combine(self):
		for section_dims in self.make_slices():
			tainted, section = self.fetch_slice(section_dims)
			if tainted:
				self.progress.write(f"Tainted: {section_dims}")
				if self.fast:
					self.taints.append((section_dims.x,
										section_dims.y,
										section_dims.x + section_dims.width - 1,
										section_dims.y + section_dims.height - 1))
					self.image.paste(section, (section_dims.x, section_dims.y))
				else:
					self.recover_section_or_skip(section_dims)
			else:
				self.progress.update(section_dims.width * section_dims.height)
				self.image.paste(section, (section_dims.x, section_dims.y))
		self.progress.close()
		if self.taint_check:
			if len(self.taints) > 0:
				tainted_image = self.image.copy()
				tainted_drawer = ImageDraw.Draw(tainted_image)
				red = ImageColor.getrgb("red")
				for taint in self.taints:
					tainted_drawer.rectangle(taint, outline=red)
				print(f"Marking tainted areas...")
				tainted_image.save(f"{self.pretty_name}-taint.png", optimize=True, compress_level=9)
			print(f"Saving {self.pretty_name}...")
			self.image.save(f"{self.pretty_name}.png", optimize=True, compress_level=9)
		else:
			self.image.save(f"{self.pretty_name}.jpg", quality=100, optimize=True, progressive=True)


if __name__ == "__main__":
	parser = ArgumentParser()
	parser.add_argument("url", help="DeviantArt picture art URL with download button unavailable")
	parser.add_argument("--fast", action="store_true", help="Skip trying to recover tainted sections")
	parser.add_argument("--minimum-chunk-size", "-m", type=int, help="Minimum chunk size", default=100)

	args = parser.parse_args()
	assert args.minimum_chunk_size > 1, "Minimum chunk size has to be 2 or greater"

	try:
		url_info = urlparse(args.url)
		if url_info.netloc != "www.deviantart.com":
			print("The domain is not dA.")
			exit(1)
		if "/art/" not in url_info.path:
			print("This URL does not look like it points to an art page.")
			exit(1)
	except ValueError:
		print(f"You have to provide a DeviantArt art URL")
		exit(1)
	# basic_data, extended_data = get_metadata(args.url)
	# combine(basic_data, extended_data)
	DAimage = DAImage(args.url, args.fast, args.minimum_chunk_size)
	DAimage.combine()
