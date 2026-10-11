#include <algorithm>
#include <array>
#include <cmath>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <stdexcept>
#include <string>
#include <vector>

#include <jpeglib.h>
#include <png.h>
#include <setjmp.h>

#include "layout.hpp"

#ifndef LINEFORGE_VERSION
#define LINEFORGE_VERSION "0.0.0"
#endif

namespace fs = std::filesystem;
static constexpr uint64_t MAX_PIXELS = 100000000ULL;

struct Image {
    int width = 0;
    int height = 0;
    std::vector<uint8_t> pixels;
};

struct Band {
    int y0 = 0, y1 = 0, x0 = 0, x1 = 0;
    uint64_t ink = 0;
    int components = 0;
    bool page_number = false;
    bool non_text_fragment = false;
    int region = 0;
    std::vector<std::string> review_reasons;
    // Keep the detected ink envelope and its valley-limited crop window
    // separate from the exported line boundary.
    int ink_y0 = -1;
    int ink_y1 = -1;
    int crop_y0 = -1;
    int crop_y1 = -1;
    int region_x0 = -1;
    int region_x1 = -1; // exclusive
    int gutter_crop_x0 = -1; // inclusive safe limit, when a left gutter exists
    int gutter_crop_x1 = -1; // inclusive safe limit, when a right gutter exists
    bool shallow_recovered = false;
};

struct RegionCropExtent {
    int gutter_left_x = -1;
    int gutter_right_x = -1;
};

static std::vector<RegionCropExtent> find_gutter_crop_extents(
    const std::vector<LayoutRegion>& regions, int image_width, bool enabled) {
    std::vector<RegionCropExtent> extents(regions.size());
    if (!enabled) return extents;

    const int minimum_gap = std::max(8, image_width / 200);
    for (size_t i = 0; i < regions.size(); ++i) {
        const auto& region = regions[i];
        int nearest_left = -1;
        int nearest_right = -1;
        for (size_t j = 0; j < regions.size(); ++j) {
            if (i == j) continue;
            const auto& other = regions[j];
            const int vertical_overlap = std::min(region.y1, other.y1) -
                                         std::max(region.y0, other.y0);
            const int smaller_height = std::min(region.y1 - region.y0,
                                                other.y1 - other.y0);
            if (vertical_overlap <= 0 || vertical_overlap * 2 < smaller_height)
                continue;

            if (other.x1 <= region.x0 && region.x0 - other.x1 >= minimum_gap &&
                (nearest_left < 0 || other.x1 > regions[static_cast<size_t>(nearest_left)].x1))
                nearest_left = static_cast<int>(j);
            if (region.x1 <= other.x0 && other.x0 - region.x1 >= minimum_gap &&
                (nearest_right < 0 || other.x0 < regions[static_cast<size_t>(nearest_right)].x0))
                nearest_right = static_cast<int>(j);
        }

        if (nearest_left >= 0) {
            const auto& left = regions[static_cast<size_t>(nearest_left)];
            extents[i].gutter_left_x = left.x1 + (region.x0 - left.x1) / 2;
        }
        if (nearest_right >= 0) {
            const auto& right = regions[static_cast<size_t>(nearest_right)];
            const int divider = region.x1 + (right.x0 - region.x1) / 2;
            extents[i].gutter_right_x = divider - 1;
        }
    }
    return extents;
}

static void require(bool ok, const std::string& message) {
    if (!ok) throw std::runtime_error(message);
}

static Image read_png(const std::string& path) {
    png_image png{};
    png.version = PNG_IMAGE_VERSION;
    require(png_image_begin_read_from_file(&png, path.c_str()) != 0,
            "cannot open PNG: " + std::string(png.message));
    if (static_cast<uint64_t>(png.width) * png.height > MAX_PIXELS) {
        png_image_free(&png);
        throw std::runtime_error("image exceeds the 100-megapixel safety limit");
    }
    png.format = PNG_FORMAT_RGBA;
    std::vector<uint8_t> rgba(PNG_IMAGE_SIZE(png));
    if (!png_image_finish_read(&png, nullptr, rgba.data(), 0, nullptr)) {
        const std::string message = png.message;
        png_image_free(&png);
        throw std::runtime_error("cannot decode PNG: " + message);
    }
    Image out;
    out.width = static_cast<int>(png.width);
    out.height = static_cast<int>(png.height);
    out.pixels.resize(static_cast<size_t>(out.width) * out.height);
    for (size_t i = 0, j = 0; i < out.pixels.size(); ++i, j += 4) {
        const unsigned a = rgba[j + 3];
        const unsigned r = (rgba[j] * a + 255u * (255u - a) + 127u) / 255u;
        const unsigned g = (rgba[j + 1] * a + 255u * (255u - a) + 127u) / 255u;
        const unsigned b = (rgba[j + 2] * a + 255u * (255u - a) + 127u) / 255u;
        out.pixels[i] = static_cast<uint8_t>((299u * r + 587u * g + 114u * b + 500u) / 1000u);
    }
    png_image_free(&png);
    return out;
}

struct JpegError {
    jpeg_error_mgr base;
    jmp_buf jump;
};

static void jpeg_fail(j_common_ptr common) {
    auto* error = reinterpret_cast<JpegError*>(common->err);
    longjmp(error->jump, 1);
}

static Image read_jpeg(const std::string& path) {
    FILE* file = std::fopen(path.c_str(), "rb");
    require(file != nullptr, "cannot open JPEG: " + path);
    jpeg_decompress_struct decoder{};
    JpegError error{};
    decoder.err = jpeg_std_error(&error.base);
    error.base.error_exit = jpeg_fail;
    unsigned char* volatile raw = nullptr;
    volatile bool created = false;
    if (setjmp(error.jump)) {
        std::free(const_cast<unsigned char*>(raw));
        if (created) jpeg_destroy_decompress(&decoder);
        std::fclose(file);
        throw std::runtime_error("cannot decode JPEG: " + path);
    }
    jpeg_create_decompress(&decoder);
    created = true;
    jpeg_stdio_src(&decoder, file);
    jpeg_read_header(&decoder, TRUE);
    if (static_cast<uint64_t>(decoder.image_width) * decoder.image_height > MAX_PIXELS) {
        jpeg_destroy_decompress(&decoder);
        created = false;
        std::fclose(file);
        throw std::runtime_error("image exceeds the 100-megapixel safety limit");
    }
    decoder.out_color_space = JCS_GRAYSCALE;
    jpeg_start_decompress(&decoder);
    const size_t count = static_cast<size_t>(decoder.output_width) * decoder.output_height;
    raw = static_cast<unsigned char*>(std::malloc(count));
    if (!raw) {
        jpeg_destroy_decompress(&decoder);
        std::fclose(file);
        throw std::runtime_error("out of memory decoding JPEG");
    }
    while (decoder.output_scanline < decoder.output_height) {
        JSAMPROW row = const_cast<unsigned char*>(raw) + static_cast<size_t>(decoder.output_scanline) * decoder.output_width;
        jpeg_read_scanlines(&decoder, &row, 1);
    }
    const int width = static_cast<int>(decoder.output_width);
    const int height = static_cast<int>(decoder.output_height);
    jpeg_finish_decompress(&decoder);
    jpeg_destroy_decompress(&decoder);
    created = false;
    std::fclose(file);
    Image out{width, height, std::vector<uint8_t>(count)};
    std::copy_n(const_cast<unsigned char*>(raw), count, out.pixels.begin());
    std::free(const_cast<unsigned char*>(raw));
    return out;
}

static Image load_image(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    require(in.good(), "input does not exist or cannot be read: " + path);
    std::array<unsigned char, 8> sig{};
    in.read(reinterpret_cast<char*>(sig.data()), sig.size());
    const auto n = in.gcount();
    if (n >= 8 && png_sig_cmp(sig.data(), 0, 8) == 0) return read_png(path);
    if (n >= 2 && sig[0] == 0xff && sig[1] == 0xd8) return read_jpeg(path);
    throw std::runtime_error("unsupported input; provide a PNG or JPEG image");
}

static void write_png_gray(const fs::path& path, const Image& image) {
    png_image png{};
    png.version = PNG_IMAGE_VERSION;
    png.width = static_cast<png_uint_32>(image.width);
    png.height = static_cast<png_uint_32>(image.height);
    png.format = PNG_FORMAT_GRAY;
    require(png_image_write_to_file(&png, path.c_str(), 0, image.pixels.data(), 0, nullptr) != 0,
            "cannot write PNG: " + std::string(png.message));
}

static int otsu_threshold(const Image& image) {
    std::array<uint64_t, 256> hist{};
    for (uint8_t p : image.pixels) ++hist[p];
    const uint64_t total = image.pixels.size();
    long double weighted = 0;
    for (int i = 0; i < 256; ++i) weighted += static_cast<long double>(i) * hist[i];
    uint64_t left_count = 0;
    long double left_sum = 0;
    long double best = -1;
    int best_t = 127;
    for (int t = 0; t < 256; ++t) {
        left_count += hist[t];
        left_sum += static_cast<long double>(t) * hist[t];
        const uint64_t right_count = total - left_count;
        if (!left_count || !right_count) continue;
        const long double left_mean = left_sum / left_count;
        const long double right_mean = (weighted - left_sum) / right_count;
        const long double score = static_cast<long double>(left_count) * right_count *
                                  (left_mean - right_mean) * (left_mean - right_mean);
        if (score > best) { best = score; best_t = t; }
    }
    return best_t;
}

static std::vector<uint32_t> horizontal_sums(const Image& im, int y, int radius) {
    std::vector<uint32_t> row(im.width);
    uint64_t sum = 0;
    const int initial_right = std::min(im.width - 1, radius);
    for (int x = 0; x <= initial_right; ++x) {
        const uint64_t p = im.pixels[static_cast<size_t>(y) * im.width + x];
        sum += p;
    }
    for (int x = 0; x < im.width; ++x) {
        row[x] = static_cast<uint32_t>(sum);
        const int remove_x = x - radius;
        const int add_x = x + radius + 1;
        if (remove_x >= 0) {
            const uint64_t p = im.pixels[static_cast<size_t>(y) * im.width + remove_x];
            sum -= p;
        }
        if (add_x < im.width) {
            const uint64_t p = im.pixels[static_cast<size_t>(y) * im.width + add_x];
            sum += p;
        }
    }
    return row;
}

static Image flatten_illumination(const Image& gray, bool debug = false) {
    if (gray.width < 32 || gray.height < 32) return gray;
    const int tile = std::clamp(std::min(gray.width, gray.height) / 18, 32, 96);
    const int grid_width = (gray.width + tile - 1) / tile;
    const int grid_height = (gray.height + tile - 1) / tile;
    std::vector<int> background(static_cast<size_t>(grid_width) * grid_height, 255);
    std::vector<int> histogram(256);
    for (int gy = 0; gy < grid_height; ++gy) for (int gx = 0; gx < grid_width; ++gx) {
        std::fill(histogram.begin(), histogram.end(), 0);
        const int x0 = gx * tile, x1 = std::min(gray.width, x0 + tile);
        const int y0 = gy * tile, y1 = std::min(gray.height, y0 + tile);
        int count = 0;
        for (int y = y0; y < y1; ++y) for (int x = x0; x < x1; ++x) {
            ++histogram[gray.pixels[static_cast<size_t>(y) * gray.width + x]];
            ++count;
        }
        const int target = std::max(1, (count * 90 + 99) / 100);
        int cumulative = 0, percentile = 255;
        for (int value = 0; value < 256; ++value) {
            cumulative += histogram[value];
            if (cumulative >= target) { percentile = value; break; }
        }
        background[static_cast<size_t>(gy) * grid_width + gx] = std::max(32, percentile);
    }

    // Smooth tile estimates to avoid creating boundaries where illumination
    // transitions cross a grid cell.
    std::vector<int> smooth_background(background.size(), 255);
    for (int gy = 0; gy < grid_height; ++gy) for (int gx = 0; gx < grid_width; ++gx) {
        int weighted = 0, weights = 0;
        for (int dy = -1; dy <= 1; ++dy) for (int dx = -1; dx <= 1; ++dx) {
            const int nx = gx + dx, ny = gy + dy;
            if (nx < 0 || nx >= grid_width || ny < 0 || ny >= grid_height) continue;
            const int weight = (dx == 0 && dy == 0) ? 4 : (dx == 0 || dy == 0) ? 2 : 1;
            weighted += background[static_cast<size_t>(ny) * grid_width + nx] * weight;
            weights += weight;
        }
        smooth_background[static_cast<size_t>(gy) * grid_width + gx] = weighted / std::max(1, weights);
    }
    std::vector<int> reference_values = smooth_background;
    const auto middle = reference_values.begin() + reference_values.size() / 2;
    std::nth_element(reference_values.begin(), middle, reference_values.end());
    const int reference = std::clamp(*middle, 96, 255);

    Image flat{gray.width, gray.height, std::vector<uint8_t>(gray.pixels.size())};
    for (int y = 0; y < gray.height; ++y) for (int x = 0; x < gray.width; ++x) {
        const double gx = std::clamp((x + 0.5) / tile - 0.5, 0.0, static_cast<double>(grid_width - 1));
        const double gy = std::clamp((y + 0.5) / tile - 0.5, 0.0, static_cast<double>(grid_height - 1));
        const int x0 = static_cast<int>(gx), y0 = static_cast<int>(gy);
        const int x1 = std::min(grid_width - 1, x0 + 1), y1 = std::min(grid_height - 1, y0 + 1);
        const double fx = gx - x0, fy = gy - y0;
        const auto at = [&](int px, int py) { return smooth_background[static_cast<size_t>(py) * grid_width + px]; };
        const double bg = std::max(32.0, (1.0 - fy) * ((1.0 - fx) * at(x0, y0) + fx * at(x1, y0)) +
                                              fy * ((1.0 - fx) * at(x0, y1) + fx * at(x1, y1)));
        const size_t pos = static_cast<size_t>(y) * gray.width + x;
        flat.pixels[pos] = static_cast<uint8_t>(std::clamp(std::lround(gray.pixels[pos] * reference / bg), 0l, 255l));
    }
    if (debug) std::cerr << "debug: illumination_flattening tile=" << tile << " reference_background=" << reference
                         << " grid=" << grid_width << "x" << grid_height << "\n";
    return flat;
}

static std::vector<uint8_t> threshold_image(const Image& gray, bool debug = false) {
    const size_t n = gray.pixels.size();
    auto [min_it, max_it] = std::minmax_element(gray.pixels.begin(), gray.pixels.end());
    std::vector<uint8_t> mask(n, 0);
    if (static_cast<int>(*max_it) - static_cast<int>(*min_it) < 3) return mask;
    const int global = otsu_threshold(gray);
    const int radius = std::clamp(std::min(gray.width, gray.height) / 50, 8, 64);
    if (debug) std::cerr << "debug: otsu=" << global << " adaptive_radius=" << radius << " grayscale_min=" << static_cast<int>(*min_it) << " grayscale_max=" << static_cast<int>(*max_it) << "\n";
    std::vector<uint64_t> vertical(gray.width, 0);
    auto add_row = [&](int y, int sign) {
        const auto sums = horizontal_sums(gray, y, radius);
        for (int x = 0; x < gray.width; ++x) {
            if (sign > 0) vertical[x] += sums[x];
            else vertical[x] -= sums[x];
        }
    };
    for (int y = 0; y <= std::min(gray.height - 1, radius); ++y) add_row(y, 1);
    bool sample_reported = false;
    for (int y = 0; y < gray.height; ++y) {
        const int y0 = std::max(0, y - radius), y1 = std::min(gray.height - 1, y + radius);
        const uint64_t rows = static_cast<uint64_t>(y1 - y0 + 1);
        for (int x = 0; x < gray.width; ++x) {
            const int x0 = std::max(0, x - radius), x1 = std::min(gray.width - 1, x + radius);
            const uint64_t count = rows * static_cast<uint64_t>(x1 - x0 + 1);
            const int mean = static_cast<int>(vertical[x] / count);
            const int local_threshold = mean - std::max(5, mean * 3 / 100);
            const int threshold = std::clamp(std::min(global + 20, local_threshold), 0, 254);
            const uint8_t p = gray.pixels[static_cast<size_t>(y) * gray.width + x];
            if (debug && !sample_reported && p < 100) {
                std::cerr << "debug: first_dark_pixel_xy=" << x << "," << y << " value=" << static_cast<int>(p) << " local_mean=" << mean << " threshold=" << threshold << "\n";
                sample_reported = true;
            }
            mask[static_cast<size_t>(y) * gray.width + x] = static_cast<uint8_t>(p <= threshold);
        }
        if (y + 1 < gray.height) {
            const int remove_y = y - radius;
            const int add_y = y + radius + 1;
            if (remove_y >= 0) add_row(remove_y, -1);
            if (add_y < gray.height) add_row(add_y, 1);
        }
    }
    return mask;
}

static void suppress_page_rules(std::vector<uint8_t>& mask, int width, int height) {
    std::vector<uint8_t> candidates(width, 0), rule_column(width, 0), binding_column(width, 0);
    for (int x = 0; x < width; ++x) {
        int run = 0, longest = 0, count = 0;
        for (int y = 0; y < height; ++y) {
            if (mask[static_cast<size_t>(y) * width + x]) { ++run; ++count; longest = std::max(longest, run); }
            else run = 0;
        }
        const bool near_outer_area = x < width * 0.25 || x > width * 0.75;
        const bool near_binding = std::abs(x - width * 0.5) <= width * 0.20;
        const bool continuous_outer_rule = near_outer_area && longest >= height * 0.40 && count >= height * 0.45;
        const bool edge_attached_stroke = (x < width * 0.015 || x >= width * 0.985) &&
            longest >= height * 0.20 && count >= height * 0.18;
        const bool interrupted_binding = near_binding && longest >= height * 0.08 && count >= height * 0.18;
        if (continuous_outer_rule || edge_attached_stroke || interrupted_binding)
            candidates[x] = 1;
    }

    // A narrow continuous stroke near the center is usually the dark book
    // binding that visually joins both pages into every horizontal band. Do
    // not erase broad central artwork (for example, a cover photograph).
    for (int x = 0; x < width;) {
        if (!candidates[x]) { ++x; continue; }
        const int start = x;
        while (x < width && candidates[x]) ++x;
        const int end = x;
        const bool outer_rule = start < width * 0.25 || end > width * 0.75;
        const int group_width = end - start;
        const bool narrow_center_rule = group_width <= std::max(8, static_cast<int>(std::lround(width * 0.08))) &&
                                        std::abs((start + end) / 2.0 - width * 0.5) <= width * 0.20;
        if (outer_rule)
            for (int xx = start; xx < end; ++xx) rule_column[xx] = 1;
        else if (narrow_center_rule)
            for (int xx = start; xx < end; ++xx) {
                rule_column[xx] = 1;
                binding_column[xx] = 1;
            }
    }
    const int padding = std::max(2, std::min(16, static_cast<int>(std::lround(width * 0.01))));
    for (int x = 0; x < width; ++x) {
        if (!rule_column[x]) continue;
        const int column_padding = binding_column[x]
            ? std::max(padding, static_cast<int>(std::lround(width * 0.025))) : padding;
        for (int xx = std::max(0, x - column_padding); xx <= std::min(width - 1, x + column_padding); ++xx)
            for (int y = 0; y < height; ++y) mask[static_cast<size_t>(y) * width + xx] = 0;
    }

    // Long scan-bed/page-edge bars create full-width false text bands. Restrict
    // this cleanup to the outer 6% and require a substantial contiguous run.
    std::vector<uint8_t> rule_row(height, 0);
    for (int y = 0; y < height; ++y) {
        int run = 0, longest = 0, count = 0;
        for (int x = 0; x < width; ++x) {
            if (mask[static_cast<size_t>(y) * width + x]) { ++run; ++count; longest = std::max(longest, run); }
            else run = 0;
        }
        const bool near_page_edge = y < height * 0.06 || y > height * 0.94;
        if (near_page_edge && longest >= width * 0.40 && count >= width * 0.45)
            rule_row[y] = 1;
    }
    const int row_padding = std::max(2, std::min(12, static_cast<int>(std::lround(height * 0.01))));
    for (int y = 0; y < height; ++y) {
        if (!rule_row[y]) continue;
        for (int yy = std::max(0, y - row_padding); yy <= std::min(height - 1, y + row_padding); ++yy)
            std::fill_n(mask.begin() + static_cast<size_t>(yy) * width, width, static_cast<uint8_t>(0));
    }
}

static void remove_tiny_components(std::vector<uint8_t>& mask, int width, int height) {
    std::vector<uint8_t> visited(mask.size(), 0);
    std::vector<uint32_t> component;
    std::vector<uint32_t> tiny_pixels;
    component.reserve(32);
    for (int y = 0; y < height; ++y) for (int x = 0; x < width; ++x) {
        const size_t start = static_cast<size_t>(y) * width + x;
        if (mask[start] == 0 || visited[start]) continue;
        component.clear();
        component.push_back(static_cast<uint32_t>(start));
        visited[start] = 1;
        for (size_t head = 0; head < component.size(); ++head) {
            const int pos = static_cast<int>(component[head]);
            const int px = pos % width, py = pos / width;
            for (int dy = -1; dy <= 1; ++dy) for (int dx = -1; dx <= 1; ++dx) {
                const int nx = px + dx, ny = py + dy;
                if (nx < 0 || nx >= width || ny < 0 || ny >= height) continue;
                const size_t next = static_cast<size_t>(ny) * width + nx;
                if (mask[next] != 0 && !visited[next]) { visited[next] = 1; component.push_back(static_cast<uint32_t>(next)); }
            }
        }
        if (component.size() == 1) mask[component.front()] = 0;
        else if (component.size() == 2) tiny_pixels.insert(tiny_pixels.end(), component.begin(), component.end());
        else for (uint32_t pos : component) mask[pos] = 2;
    }

    // Small paired marks close to substantial glyph components are often
    // Ethiopic diacritics; a detached singleton is more likely scan dust. The
    // original grayscale pixels remain in the padded crop either way.
    const int mark_radius = std::clamp(std::min(width, height) / 140, 5, 12);
    for (uint32_t pos : tiny_pixels) {
        const int x = static_cast<int>(pos % static_cast<uint32_t>(width));
        const int y = static_cast<int>(pos / static_cast<uint32_t>(width));
        bool near_glyph = false;
        for (int yy = std::max(0, y - mark_radius); yy <= std::min(height - 1, y + mark_radius) && !near_glyph; ++yy)
            for (int xx = std::max(0, x - mark_radius); xx <= std::min(width - 1, x + mark_radius); ++xx)
                if (mask[static_cast<size_t>(yy) * width + xx] == 2) { near_glyph = true; break; }
        if (!near_glyph) mask[pos] = 0;
        else mask[pos] = 1;
    }
}

static std::vector<uint8_t> rotate_mask(const std::vector<uint8_t>& src, int w, int h, double degrees) {
    if (std::abs(degrees) < 1e-8) return src;
    std::vector<uint8_t> dst(src.size(), 0);
    const double a = degrees * M_PI / 180.0, c = std::cos(a), s = std::sin(a);
    const double cx = (w - 1) / 2.0, cy = (h - 1) / 2.0;
    for (int y = 0; y < h; ++y) for (int x = 0; x < w; ++x) {
        const double dx = x - cx, dy = y - cy;
        const int sx = static_cast<int>(std::lround(c * dx + s * dy + cx));
        const int sy = static_cast<int>(std::lround(-s * dx + c * dy + cy));
        if (sx >= 0 && sx < w && sy >= 0 && sy < h)
            dst[static_cast<size_t>(y) * w + x] = src[static_cast<size_t>(sy) * w + sx];
    }
    return dst;
}

static Image rotate_gray(const Image& src, double degrees) {
    if (std::abs(degrees) < 1e-8) return src;
    Image dst{src.width, src.height, std::vector<uint8_t>(src.pixels.size(), 255)};
    const double a = degrees * M_PI / 180.0, c = std::cos(a), s = std::sin(a);
    const double cx = (src.width - 1) / 2.0, cy = (src.height - 1) / 2.0;
    for (int y = 0; y < src.height; ++y) for (int x = 0; x < src.width; ++x) {
        const double dx = x - cx, dy = y - cy;
        const double sx = c * dx + s * dy + cx, sy = -s * dx + c * dy + cy;
        if (sx < 0 || sy < 0 || sx >= src.width - 1 || sy >= src.height - 1) continue;
        const int x0 = static_cast<int>(sx), y0 = static_cast<int>(sy);
        const double fx = sx - x0, fy = sy - y0;
        const auto at = [&](int px, int py) { return src.pixels[static_cast<size_t>(py) * src.width + px]; };
        const double value = (1 - fy) * ((1 - fx) * at(x0, y0) + fx * at(x0 + 1, y0)) +
                             fy * ((1 - fx) * at(x0, y0 + 1) + fx * at(x0 + 1, y0 + 1));
        dst.pixels[static_cast<size_t>(y) * src.width + x] = static_cast<uint8_t>(std::clamp(std::lround(value), 0l, 255l));
    }
    return dst;
}

struct SamplePoint { int x; int y; };

static double projection_score(const std::vector<SamplePoint>& points, int w, int h, int step, double degrees) {
    const int sw = (w + step - 1) / step, sh = (h + step - 1) / step;
    std::vector<uint32_t> rows(sh, 0);
    const double a = degrees * M_PI / 180.0, c = std::cos(a), s = std::sin(a);
    const double cx = (w - 1) / 2.0, cy = (h - 1) / 2.0;
    uint64_t ink = 0;
    for (const auto& point : points) {
        const double dx = point.x - cx, dy = point.y - cy;
        const int yy = static_cast<int>(std::lround((s * dx + c * dy + cy) / step));
        const int xx = static_cast<int>(std::lround((c * dx - s * dy + cx) / step));
        if (xx >= 0 && xx < sw && yy >= 0 && yy < sh) { ++rows[yy]; ++ink; }
    }
    if (!ink) return 0;
    long double sq = 0;
    for (uint32_t v : rows) sq += static_cast<long double>(v) * v;
    return static_cast<double>(sq / (static_cast<long double>(ink) * ink));
}

static double estimate_deskew(const std::vector<uint8_t>& mask, int w, int h) {
    if (w < 80 || h < 40) return 0.0;
    const int step = std::max(1, std::max(w, h) / 1200);
    const size_t sample_count = static_cast<size_t>((w + step - 1) / step) * ((h + step - 1) / step);
    std::vector<SamplePoint> points;
    points.reserve(std::min<size_t>(sample_count / 20, 1500000));
    for (int y = 0; y < h; y += step) for (int x = 0; x < w; x += step)
        if (mask[static_cast<size_t>(y) * w + x]) points.push_back({x, y});
    if (points.empty()) return 0.0;
    const double base = projection_score(points, w, h, step, 0.0);
    double best = base, best_angle = 0.0;
    for (int i = -20; i <= 20; ++i) {
        if (i == 0) continue;
        const double angle = i * 0.25;
        const double score = projection_score(points, w, h, step, angle);
        if (score > best) { best = score; best_angle = angle; }
    }
    return (best > base * 1.004) ? best_angle : 0.0;
}

static int component_count(const std::vector<uint8_t>& mask, int w, int x0, int y0, int x1, int y1) {
    const int rw = x1 - x0 + 1, rh = y1 - y0 + 1;
    std::vector<uint8_t> seen(static_cast<size_t>(rw) * rh, 0);
    std::vector<int> queue;
    int count = 0;
    for (int y = y0; y <= y1; ++y) for (int x = x0; x <= x1; ++x) {
        const size_t local = static_cast<size_t>(y - y0) * rw + (x - x0);
        if (seen[local] || !mask[static_cast<size_t>(y) * w + x]) continue;
        ++count;
        queue.clear(); queue.push_back(static_cast<int>(local)); seen[local] = 1;
        for (size_t head = 0; head < queue.size(); ++head) {
            const int pos = queue[head], px = pos % rw, py = pos / rw;
            for (int oy = -1; oy <= 1; ++oy) for (int ox = -1; ox <= 1; ++ox) {
                const int nx = px + ox, ny = py + oy;
                if (nx < 0 || nx >= rw || ny < 0 || ny >= rh) continue;
                const size_t ni = static_cast<size_t>(ny) * rw + nx;
                if (seen[ni] || !mask[static_cast<size_t>(y0 + ny) * w + (x0 + nx)]) continue;
                seen[ni] = 1; queue.push_back(static_cast<int>(ni));
            }
        }
    }
    return count;
}

static std::vector<Band> find_bands(const std::vector<uint8_t>& mask, int w, int h, bool debug = false) {
    std::vector<uint32_t> rows(h, 0);
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x) rows[y] += mask[static_cast<size_t>(y) * w + x] != 0;
    std::vector<int> smooth(h, 0);
    for (int y = 0; y < h; ++y) {
        for (int dy = -1; dy <= 1; ++dy) if (y + dy >= 0 && y + dy < h) smooth[y] += rows[y + dy];
    }
    std::vector<uint32_t> sorted_rows = rows;
    std::nth_element(sorted_rows.begin(), sorted_rows.begin() + sorted_rows.size() / 2, sorted_rows.end());
    const int median_row_ink = static_cast<int>(sorted_rows[sorted_rows.size() / 2]);
    const int occupied_rows = static_cast<int>(std::count_if(rows.begin(), rows.end(), [](uint32_t v) { return v > 0; }));
    std::vector<uint32_t> active_rows;
    active_rows.reserve(static_cast<size_t>(occupied_rows));
    for (const uint32_t row_ink : rows) if (row_ink > 0) active_rows.push_back(row_ink);
    int median_active_row_ink = 0;
    if (!active_rows.empty()) {
        const auto middle = active_rows.begin() + active_rows.size() / 2;
        std::nth_element(active_rows.begin(), middle, active_rows.end());
        median_active_row_ink = static_cast<int>(*middle);
    }
    const bool noise_limited_dense_page = h >= std::max(80, w / 16) && occupied_rows > h * 0.40 &&
        median_row_ink >= std::max(4, w / 200) && median_row_ink <= w * 0.25;
    const int dense_row_threshold = noise_limited_dense_page ? median_row_ink * 2 : 0;
    const bool sparse_text_block = h >= std::max(120, w / 10) && occupied_rows > h * 0.35 &&
        median_row_ink == 0 && median_active_row_ink >= 3;
    const int sparse_row_threshold = sparse_text_block ? median_active_row_ink * 3 : 0;
    const int row_threshold = std::max({3, static_cast<int>(std::ceil(w * 0.004)), dense_row_threshold, sparse_row_threshold});
    if (debug)
        std::cerr << "debug: line_rows width=" << w << " height=" << h << " occupied=" << occupied_rows
                  << " median_ink=" << median_row_ink << " median_active_ink=" << median_active_row_ink
                  << " row_threshold=" << row_threshold << "\n";
    std::vector<std::pair<int, int>> raw;
    for (int y = 0; y < h;) {
        if (smooth[y] < row_threshold) { ++y; continue; }
        const int start = y;
        while (y + 1 < h && smooth[y + 1] >= row_threshold) ++y;
        raw.emplace_back(start, y);
        ++y;
    }
    if (debug) {
        std::cerr << "debug: raw_row_runs=";
        for (const auto& run : raw) std::cerr << "[" << run.first << "," << run.second + 1 << ")";
        std::cerr << "\n";
    }
    std::vector<int> heights;
    for (auto [a, b] : raw) if (b - a + 1 >= 3) heights.push_back(b - a + 1);
    int typical = 12;
    if (!heights.empty()) {
        std::sort(heights.begin(), heights.end());
        // The median can be inflated when two or more tightly spaced rows have
        // already joined into one raw projection run. A lower quartile is a
        // more stable estimate of an individual glyph row in dense scans.
        typical = heights[(heights.size() - 1) / 4];
    }
    if (typical > h * 0.20) typical = std::max(8, h / 24);
    std::vector<int> pitches;
    for (size_t i = 1; i < raw.size(); ++i) {
        const int pitch = (raw[i].first + raw[i].second - raw[i - 1].first - raw[i - 1].second) / 2;
        if (pitch >= std::max(3, static_cast<int>(std::lround(typical * 0.85))) &&
            pitch <= typical * 5) pitches.push_back(pitch);
    }
    int line_pitch = typical * 2;
    if (!pitches.empty()) {
        std::sort(pitches.begin(), pitches.end());
        line_pitch = pitches[(pitches.size() - 1) / 4];
    }
    const bool compact_line_region = h < std::max(80, w / 16);
    const int merge_gap = compact_line_region ? h :
        std::clamp(static_cast<int>(std::lround(typical * 0.24)), 2, 14);
    std::vector<std::pair<int, int>> merged;
    for (auto run : raw) {
        if (!merged.empty() && run.first - merged.back().second - 1 <= merge_gap) merged.back().second = run.second;
        else merged.push_back(run);
    }
    std::vector<std::pair<int, int>> refined;
    // In a dense block, true inter-line valleys can retain faint Ethiopic
    // marks and scan texture. Use a small fraction of local row activity rather
    // than requiring an almost-empty row; the oversized-run and minimum-piece
    // guards below keep ordinary glyph bowls from becoming split points.
    const int low_ink_limit = std::max({2, median_active_row_ink / 8, row_threshold / 2});
    // In tight line spacing the valley can be only one raster row wide; the
    // local-depth threshold plus minimum piece height provide the safeguards.
    const int min_valley = 1;
    const int min_fragment_height = std::max(5, static_cast<int>(std::lround(typical * 0.58)));
    const int oversized_limit = std::max(static_cast<int>(std::lround(typical * 1.8)),
                                         static_cast<int>(std::lround(line_pitch * 1.05)));
    for (const auto& run : merged) {
        const int run_height = run.second - run.first + 1;
        if (debug && run_height > oversized_limit) {
            std::vector<uint32_t> profile(rows.begin() + run.first, rows.begin() + run.second + 1);
            std::sort(profile.begin(), profile.end());
            std::cerr << "debug: oversized_row_profile y=[" << run.first << "," << run.second + 1
                      << ") height=" << run_height << " typical=" << typical << " pitch=" << line_pitch
                      << " low_limit=" << low_ink_limit << " q10=" << profile[profile.size() / 10]
                      << " median=" << profile[profile.size() / 2] << " min=" << profile.front() << " minima=";
            const int neighborhood = std::max(2, typical / 3);
            int printed = 0;
            for (int y = run.first + neighborhood; y <= run.second - neighborhood && printed < 20; ++y) {
                const uint32_t value = rows[static_cast<size_t>(y)];
                bool minimum = true;
                for (int d = 1; d <= neighborhood; ++d)
                    if (rows[static_cast<size_t>(y - d)] < value || rows[static_cast<size_t>(y + d)] < value) {
                        minimum = false; break;
                    }
                if (minimum) { std::cerr << y << ":" << value << " "; ++printed; }
            }
            std::cerr << "\n";
        }
        if (h < 80 || run_height <= oversized_limit) { refined.push_back(run); continue; }
        int segment_start = run.first;
        std::vector<std::pair<int, int>> pieces;
        for (int y = run.first; y <= run.second;) {
            if (rows[static_cast<size_t>(y)] > static_cast<uint32_t>(low_ink_limit)) { ++y; continue; }
            const int gap_start = y;
            while (y <= run.second && rows[static_cast<size_t>(y)] <= static_cast<uint32_t>(low_ink_limit)) ++y;
            const int gap_end = y - 1;
            if (gap_end - gap_start + 1 < min_valley) continue;
            if (gap_start - segment_start >= min_fragment_height && run.second - gap_end >= min_fragment_height) {
                pieces.emplace_back(segment_start, gap_start - 1);
                segment_start = gap_end + 1;
            }
        }
        if (pieces.empty()) refined.push_back(run);
        else {
            pieces.emplace_back(segment_start, run.second);
            for (const auto& piece : pieces) {
                if (!refined.empty() && piece.second - piece.first + 1 < min_fragment_height &&
                    refined.back().second + min_valley >= piece.first)
                    refined.back().second = piece.second;
                else refined.push_back(piece);
            }
        }
    }
    if (debug) {
        std::cerr << "debug: refined_row_runs=";
        for (const auto& run : refined) std::cerr << "[" << run.first << "," << run.second + 1 << ")";
        std::cerr << "\n";
    }

    std::vector<Band> bands;
    for (auto [y0, y1] : refined) {
        const int bh = y1 - y0 + 1;
        if (bh < 2) continue;
        int x0 = w, x1 = -1;
        uint64_t ink = 0;
        std::vector<uint32_t> columns(w, 0);
        for (int y = y0; y <= y1; ++y) for (int x = 0; x < w; ++x) {
            if (mask[static_cast<size_t>(y) * w + x]) { ++columns[x]; ++ink; }
        }
        // Any supported foreground column matters: the small detached marks
        // used in Ethiopic writing can occupy only a few pixels.
        const int x_threshold = 1;
        for (int x = 0; x < w; ++x) if (columns[x] >= static_cast<uint32_t>(x_threshold)) { x0 = std::min(x0, x); x1 = std::max(x1, x); }
        if (x1 < x0 || ink < std::max<uint64_t>(8, static_cast<uint64_t>(w * 0.003))) continue;
        const int ncomp = component_count(mask, w, x0, y0, x1, y1);
        if (ncomp == 0) continue;
        bands.push_back(Band{y0, y1, x0, x1, ink, ncomp, false, false, 0, {}});
    }
    return bands;
}

static void mark_likely_page_numbers(std::vector<Band>& bands, int w, int h, bool enabled) {
    if (!enabled || bands.size() < 2) return;
    std::vector<int> wide_heights;
    for (const auto& b : bands) if (b.x1 - b.x0 + 1 >= w * 0.20) wide_heights.push_back(b.y1 - b.y0 + 1);
    if (wide_heights.empty()) return;
    std::sort(wide_heights.begin(), wide_heights.end());
    const double median_h = wide_heights[wide_heights.size() / 2];
    for (size_t i = 0; i < bands.size(); ++i) {
        const auto& b = bands[i];
        const double bw = b.x1 - b.x0 + 1, bh = b.y1 - b.y0 + 1;
        const double center = (b.x0 + b.x1) / 2.0;
        const bool compact = bw <= w * 0.16 && bh <= std::max(median_h * 2.0, h * 0.025) && b.components <= 8;
        const bool centered_on_page = std::min({std::abs(center - w * 0.25),
                                                std::abs(center - w * 0.50),
                                                std::abs(center - w * 0.75)}) <= w * 0.11;
        const bool lower_half = b.y0 >= h * 0.65;
        bool terminal_in_lane = true;
        for (size_t j = 0; j < bands.size(); ++j) {
            if (i == j) continue;
            const auto& later = bands[j];
            const double later_center = (later.x0 + later.x1) / 2.0;
            if (later.y0 > b.y1 + 1 && std::abs(later_center - center) <= w * 0.18) {
                terminal_in_lane = false;
                break;
            }
        }
        if (compact && centered_on_page && lower_half && terminal_in_lane)
            bands[i].page_number = true;
    }
}

static void mark_likely_nontext_fragments(std::vector<Band>& bands, int w, int h) {
    for (auto& b : bands) {
        const int bw = b.x1 - b.x0 + 1, bh = b.y1 - b.y0 + 1;
        if (bw <= 0 || bh <= 0) continue;
        // Leave compact footer-like candidates to the existing page-number review rule.
        const double center = (b.x0 + b.x1) / 2.0;
        const bool footer_lane = std::min({std::abs(center - w * 0.25), std::abs(center - w * 0.50),
                                           std::abs(center - w * 0.75)}) <= w * 0.11;
        if (b.y0 >= h * 0.40 && bw <= w * 0.16 && b.components <= 8 && footer_lane) continue;
        const bool touches_side = b.x0 <= w * 0.01 || b.x1 >= w * 0.99;
        const bool side_scan_fragment = touches_side && bw <= w * 0.12 && bh >= std::max(8, static_cast<int>(h * 0.02));
        const bool long_narrow_binding = bw <= w * 0.05 && bh >= h * 0.15 && bh >= bw * 4;
        bool attached_short_mark = false;
        if (bh <= std::max(6, static_cast<int>(h * 0.01))) {
            for (const auto& other : bands) {
                if (&other == &b || other.region != b.region || other.non_text_fragment || other.page_number) continue;
                const int other_h = other.y1 - other.y0 + 1;
                const int gap = other.y0 > b.y1 ? other.y0 - b.y1 - 1 :
                                b.y0 > other.y1 ? b.y0 - other.y1 - 1 : -1;
                if (gap < 0 || gap > std::max(2, static_cast<int>(std::lround(bh * 0.35))) || other_h < bh * 1.5) continue;
                const int overlap = std::max(0, std::min(b.x1, other.x1) - std::max(b.x0, other.x0) + 1);
                const double ratio = static_cast<double>(overlap) / std::max(1, std::min(bw, other.x1 - other.x0 + 1));
                if (ratio >= 0.60) { attached_short_mark = true; break; }
            }
        }
        const bool thin_horizontal_rule = bh <= std::max(4, static_cast<int>(h * 0.005)) &&
                                          bw >= w * 0.12 &&
                                          (b.ink * 4 <= static_cast<uint64_t>(bw) ||
                                           (b.components <= 2 && static_cast<double>(b.ink) >=
                                            static_cast<double>(bw) * bh * 0.55));
        const bool isolated_speck = bw <= w * 0.02 && bh <= h * 0.015 && b.components <= 3;
        b.non_text_fragment = side_scan_fragment || long_narrow_binding ||
                              (thin_horizontal_rule && !attached_short_mark) || isolated_speck;
    }
}

static void merge_short_line_fragments(std::vector<Band>& bands, int height) {
    std::vector<int> typical_candidates;
    for (const auto& b : bands) {
        if (b.non_text_fragment || b.page_number) continue;
        const int bh = b.y1 - b.y0 + 1;
        if (bh >= 8 && bh <= height * 0.08) typical_candidates.push_back(bh);
    }
    int typical_height = 12;
    if (!typical_candidates.empty()) {
        auto middle = typical_candidates.begin() + typical_candidates.size() / 2;
        std::nth_element(typical_candidates.begin(), middle, typical_candidates.end());
        typical_height = *middle;
    }
    const int fragment_height = std::max(4, static_cast<int>(std::lround(typical_height * 0.55)));
    const int fragment_gap = std::max(3, static_cast<int>(std::lround(typical_height * 0.75)));
    std::stable_sort(bands.begin(), bands.end(), [](const Band& a, const Band& b) {
        if (a.region != b.region) return a.region < b.region;
        if (a.y0 != b.y0) return a.y0 < b.y0;
        return a.x0 < b.x0;
    });
    for (size_t i = 0; i + 1 < bands.size();) {
        auto& a = bands[i];
        auto& b = bands[i + 1];
        const int ah = a.y1 - a.y0 + 1, bh = b.y1 - b.y0 + 1;
        const int aw = a.x1 - a.x0 + 1, bw = b.x1 - b.x0 + 1;
        const int gap = b.y0 - a.y1 - 1;
        const int overlap = std::max(0, std::min(a.x1, b.x1) - std::max(a.x0, b.x0) + 1);
        const double overlap_ratio = static_cast<double>(overlap) / std::max(1, std::min(aw, bw));
        const bool small_pair = ah <= fragment_height && bh <= fragment_height && gap <= fragment_gap;
        const bool detached_mark_pair = (ah <= fragment_height || bh <= fragment_height) &&
            gap <= std::max(2, static_cast<int>(std::lround(typical_height * 0.25))) &&
            b.y1 - a.y0 + 1 <= typical_height * 2.8;
        const bool join = a.region == b.region && !a.non_text_fragment && !b.non_text_fragment &&
            !a.page_number && !b.page_number && (small_pair || detached_mark_pair) &&
            gap >= 0 && overlap_ratio >= 0.60;
        if (!join) { ++i; continue; }
        a.x0 = std::min(a.x0, b.x0); a.y0 = std::min(a.y0, b.y0);
        a.x1 = std::max(a.x1, b.x1); a.y1 = std::max(a.y1, b.y1);
        a.ink += b.ink; a.components += b.components;
        bands.erase(bands.begin() + static_cast<std::ptrdiff_t>(i + 1));
    }
}

static void mark_uncertain_bands(std::vector<Band>& bands, int width, int height) {
    size_t region_count = 1;
    for (const auto& b : bands) region_count = std::max(region_count, static_cast<size_t>(std::max(0, b.region)) + 1);
    std::vector<std::vector<int>> heights_by_region(region_count);
    for (const auto& b : bands) {
        if (b.non_text_fragment || b.page_number) continue;
        const int bh = b.y1 - b.y0 + 1;
        // Exclude likely multi-line outliers when estimating the local glyph
        // scale; otherwise one merged band could teach its own region that it
        // is normal-sized.
        if (bh >= 4 && bh <= height * 0.08)
            heights_by_region[static_cast<size_t>(std::max(0, b.region))].push_back(bh);
    }
    std::vector<int> typical_height(region_count, 12);
    for (size_t region = 0; region < heights_by_region.size(); ++region) {
        auto& values = heights_by_region[region];
        if (values.empty()) continue;
        auto middle = values.begin() + values.size() / 2;
        std::nth_element(values.begin(), middle, values.end());
        typical_height[region] = *middle;
    }
    const int edge_x = std::max(2, static_cast<int>(std::lround(width * 0.005)));
    const int edge_y = std::max(2, static_cast<int>(std::lround(height * 0.008)));
    for (auto& b : bands) {
        if (b.non_text_fragment || b.page_number) continue;
        const int bw = b.x1 - b.x0 + 1, bh = b.y1 - b.y0 + 1;
        const int region_height = typical_height[static_cast<size_t>(std::max(0, b.region))];
        const int oversized = std::max(static_cast<int>(std::lround(region_height * 1.8)),
                                       static_cast<int>(std::lround(height * 0.025)));
        if (bh > oversized) b.review_reasons.emplace_back("possible_merged_or_oversized_line");

        bool close_vertical_fragment = false;
        for (const auto& other : bands) {
            if (&other == &b || other.region != b.region || other.non_text_fragment || other.page_number) continue;
            const int gap = other.y0 > b.y1 ? other.y0 - b.y1 - 1 :
                            b.y0 > other.y1 ? b.y0 - other.y1 - 1 : -1;
            if (gap < 0 || gap > std::max(2, static_cast<int>(std::lround(region_height * 0.35)))) continue;
            const int overlap = std::max(0, std::min(b.x1, other.x1) - std::max(b.x0, other.x0) + 1);
            const double ratio = static_cast<double>(overlap) / std::max(1, std::min(bw, other.x1 - other.x0 + 1));
            if (ratio >= 0.60) { close_vertical_fragment = true; break; }
        }
        const bool unusually_short = bh < std::max(4, static_cast<int>(std::lround(region_height * 0.65)));
        if (bw < width * 0.16 && b.components <= 5 && (unusually_short || close_vertical_fragment))
            b.review_reasons.emplace_back("short_or_fragmentary_text_candidate");
        const int local_width = b.region_x1 > b.region_x0 ? b.region_x1 - b.region_x0 : width;
        const bool substantial_text_support = b.components >= 4 &&
            b.ink >= std::max<uint64_t>(32, static_cast<uint64_t>(std::lround(bw * 0.02)));
        if (b.shallow_recovered) {
            b.review_reasons.emplace_back("shallow_candidate_expanded_from_local_ink");
        } else if (bh < std::max(4, static_cast<int>(std::lround(region_height * 0.60))) &&
                   bw >= std::max(24, static_cast<int>(std::lround(local_width * 0.18))) &&
                   substantial_text_support) {
            b.review_reasons.emplace_back("suspiciously_shallow_candidate");
        }
        if (b.x0 <= edge_x || b.y0 <= edge_y || b.x1 >= width - edge_x || b.y1 >= height - edge_y)
            b.review_reasons.emplace_back("possible_page_edge_clipping");
    }
}

static int row_ink_in_xrange(const std::vector<uint8_t>& mask, int width,
                             int y, int x0, int x1) {
    int ink = 0;
    const size_t row = static_cast<size_t>(y) * width;
    for (int x = std::max(0, x0); x <= std::min(width - 1, x1); ++x)
        ink += mask[row + static_cast<size_t>(x)] != 0;
    return ink;
}

static int band_peak_row_ink(const std::vector<uint8_t>& mask, int width,
                             const Band& band) {
    int peak = 0;
    for (int y = band.y0; y <= band.y1; ++y)
        peak = std::max(peak, row_ink_in_xrange(mask, width, y, band.x0, band.x1));
    return peak;
}

static int band_dark_reference(const std::vector<uint8_t>& gray,
                               const std::vector<uint8_t>& mask,
                               int width, const Band& band) {
    std::array<int, 256> histogram{};
    int total = 0;
    for (int y = band.y0; y <= band.y1; ++y) {
        const size_t row = static_cast<size_t>(y) * width;
        for (int x = std::max(0, band.x0); x <= std::min(width - 1, band.x1); ++x) {
            const size_t position = row + static_cast<size_t>(x);
            if (!mask[position]) continue;
            ++histogram[gray[position]];
            ++total;
        }
    }
    if (total == 0) return 0;
    const int target = (total - 1) / 2;
    int cumulative = 0;
    for (int value = 0; value < 256; ++value) {
        cumulative += histogram[static_cast<size_t>(value)];
        if (cumulative > target) return value;
    }
    return 0;
}

static int faint_row_ink_in_xrange(const std::vector<uint8_t>& gray, int width,
                                   int y, int x0, int x1, int dark_reference) {
    const int left = std::max(0, x0), right = std::min(width - 1, x1);
    if (y < 0 || left > right) return 0;
    std::array<int, 256> histogram{};
    const size_t row = static_cast<size_t>(y) * width;
    const int count = right - left + 1;
    for (int x = left; x <= right; ++x)
        ++histogram[gray[row + static_cast<size_t>(x)]];
    const int background_rank = static_cast<int>((static_cast<uint64_t>(count) * 85 + 99) / 100);
    int background = 255, cumulative = 0;
    for (int value = 0; value < 256; ++value) {
        cumulative += histogram[static_cast<size_t>(value)];
        if (cumulative >= background_rank) { background = value; break; }
    }
    const int contrast = std::max(0, background - dark_reference);
    const int minimum_weak_threshold = std::min(224, dark_reference + 4);
    const int weak_threshold = std::clamp(
        background - std::max(8, static_cast<int>(std::lround(contrast * 0.40))),
        minimum_weak_threshold, 224);
    int support = 0;
    for (int value = 0; value <= weak_threshold; ++value)
        support += histogram[static_cast<size_t>(value)];
    return support;
}

static int median_value(std::vector<int> values, int fallback) {
    if (values.empty()) return fallback;
    const auto middle = values.begin() + static_cast<std::ptrdiff_t>(values.size() / 2);
    std::nth_element(values.begin(), middle, values.end());
    return *middle;
}

static size_t recover_shallow_bands(std::vector<Band>& bands,
                                    const std::vector<uint8_t>& mask,
                                    const std::vector<uint8_t>& gray,
                                    int width, int height, bool debug = false) {
    if (bands.size() < 4 || width <= 0 || height <= 0) return 0;
    std::vector<size_t> order(bands.size());
    std::iota(order.begin(), order.end(), 0);
    std::stable_sort(order.begin(), order.end(), [&](size_t a, size_t b) {
        if (bands[a].y0 != bands[b].y0) return bands[a].y0 < bands[b].y0;
        return bands[a].x0 < bands[b].x0;
    });

    std::vector<int> all_heights;
    for (const auto& band : bands) {
        const int band_height = band.y1 - band.y0 + 1;
        if (!band.non_text_fragment && !band.page_number && band_height >= 4 && band_height <= height * 0.08)
            all_heights.push_back(band_height);
    }
    const int overall_scale = median_value(std::move(all_heights), 12);
    size_t recovered = 0;
    for (size_t position = 0; position < order.size(); ++position) {
        auto& band = bands[order[position]];
        if (band.non_text_fragment || band.page_number) continue;
        const int original_height = band.y1 - band.y0 + 1;
        if (original_height < 4 || original_height > height * 0.08) continue;
        const int original_y0 = band.y0;
        const int original_y1_exclusive = band.y1 + 1;

        std::vector<int> neighbor_heights;
        for (int distance = 1; distance <= 3; ++distance) {
            for (const int direction : {-1, 1}) {
                const auto neighbor_position = static_cast<long long>(position) + direction * distance;
                if (neighbor_position < 0 || neighbor_position >= static_cast<long long>(order.size())) continue;
                const auto& neighbor = bands[order[static_cast<size_t>(neighbor_position)]];
                const int neighbor_height = neighbor.y1 - neighbor.y0 + 1;
                if (!neighbor.non_text_fragment && !neighbor.page_number && neighbor_height >= 6 &&
                    neighbor_height <= height * 0.08)
                    neighbor_heights.push_back(neighbor_height);
            }
        }
        const int local_scale = median_value(std::move(neighbor_heights), overall_scale);
        if (local_scale < 6 || original_height >= std::lround(local_scale * 0.82)) continue;
        const int box_width = band.x1 - band.x0 + 1;
        const uint64_t minimum_ink = std::max<uint64_t>(32, static_cast<uint64_t>(std::lround(box_width * 0.02)));
        if (box_width < std::max(24, static_cast<int>(std::lround(width * 0.12))) ||
            band.components < 4 || band.ink < minimum_ink)
            continue;

        const int peak = band_peak_row_ink(mask, width, band);
        if (peak < 8) continue;
        const int dark_reference = band_dark_reference(gray, mask, width, band);
        const int support_floor = std::max(4, static_cast<int>(std::ceil(peak * 0.015)));
        const int max_expansion = std::max(2, static_cast<int>(std::lround(local_scale * 0.90)));
        const int max_internal_valley = std::max(2, static_cast<int>(std::lround(local_scale * 0.18)));
        int lower_limit = std::max(0, band.y0 - max_expansion);
        int upper_limit = std::min(height, band.y1 + 1 + max_expansion);
        if (position > 0) {
            const auto& previous = bands[order[position - 1]];
            const int previous_end = previous.y1 + 1;
            if (previous_end >= band.y0) lower_limit = band.y0;
            else lower_limit = std::max(lower_limit, previous_end + (band.y0 - previous_end + 1) / 2);
        }
        if (position + 1 < order.size()) {
            const auto& next = bands[order[position + 1]];
            const int current_end = band.y1 + 1;
            if (next.y0 <= current_end) upper_limit = current_end;
            else upper_limit = std::min(upper_limit, current_end + (next.y0 - current_end + 1) / 2);
        }

        int expanded_y0 = band.y0;
        int upper_valley = 0;
        for (int y = band.y0 - 1; y >= lower_limit; --y) {
            const int strong_support = row_ink_in_xrange(mask, width, y, band.x0, band.x1);
            const int faint_support = strong_support < support_floor
                ? faint_row_ink_in_xrange(gray, width, y, band.x0, band.x1, dark_reference) : 0;
            const int support = std::max(strong_support, faint_support);
            if (debug) std::cerr << "debug: shallow_support side=top y=" << y << " strong=" << strong_support
                                 << " faint=" << faint_support << " threshold=" << support_floor << "\n";
            if (support >= support_floor) { expanded_y0 = y; upper_valley = 0; }
            else {
                if (support > 0 && support * 2 >= support_floor) expanded_y0 = y;
                if (++upper_valley > max_internal_valley) break;
            }
        }
        int expanded_y1_exclusive = band.y1 + 1;
        int lower_valley = 0;
        for (int y = band.y1 + 1; y < upper_limit; ++y) {
            const int strong_support = row_ink_in_xrange(mask, width, y, band.x0, band.x1);
            const int faint_support = strong_support < support_floor
                ? faint_row_ink_in_xrange(gray, width, y, band.x0, band.x1, dark_reference) : 0;
            const int support = std::max(strong_support, faint_support);
            if (debug) std::cerr << "debug: shallow_support side=bottom y=" << y << " strong=" << strong_support
                                 << " faint=" << faint_support << " threshold=" << support_floor << "\n";
            if (support >= support_floor) { expanded_y1_exclusive = y + 1; lower_valley = 0; }
            else {
                if (support > 0 && support * 2 >= support_floor) expanded_y1_exclusive = y + 1;
                if (++lower_valley > max_internal_valley) break;
            }
        }
        const int expanded_height = expanded_y1_exclusive - expanded_y0;
        if (expanded_height < original_height + 3) continue;

        uint64_t expanded_ink = 0;
        for (int y = expanded_y0; y < expanded_y1_exclusive; ++y)
            expanded_ink += static_cast<uint64_t>(row_ink_in_xrange(mask, width, y, band.x0, band.x1));
        band.y0 = expanded_y0;
        band.y1 = expanded_y1_exclusive - 1;
        band.ink = expanded_ink;
        band.shallow_recovered = true;
        ++recovered;
        if (debug)
            std::cerr << "debug: shallow_band_expanded original=[" << original_y0 << ","
                      << original_y1_exclusive << ") expanded=[" << band.y0 << "," << band.y1 + 1
                      << ") local_scale=" << local_scale << " support_floor=" << support_floor << "\n";
    }
    return recovered;
}

static void refine_vertical_boundaries(std::vector<Band>& bands,
                                       const std::vector<uint8_t>& mask,
                                       int width, int height, bool debug = false) {
    size_t region_count = 1;
    for (const auto& band : bands)
        region_count = std::max(region_count,
            static_cast<size_t>(std::max(0, band.region)) + 1);
    std::vector<std::vector<size_t>> by_region(region_count);
    for (size_t i = 0; i < bands.size(); ++i) {
        auto& band = bands[i];
        band.ink_y0 = band.y0;
        band.ink_y1 = band.y1;
        if (!band.non_text_fragment && !band.page_number)
            by_region[static_cast<size_t>(std::max(0, band.region))].push_back(i);
    }

    for (auto& indices : by_region) {
        std::stable_sort(indices.begin(), indices.end(), [&](size_t a, size_t b) {
            if (bands[a].y0 != bands[b].y0) return bands[a].y0 < bands[b].y0;
            return bands[a].x0 < bands[b].x0;
        });
        if (indices.empty()) continue;

        std::vector<int> normal_heights;
        for (size_t index : indices) {
            const int h = bands[index].y1 - bands[index].y0 + 1;
            if (h >= 3 && h <= height * 0.08) normal_heights.push_back(h);
        }
        const int typical_height = std::max(3, median_value(std::move(normal_heights), 12));

        std::vector<int> pitches;
        for (size_t i = 1; i < indices.size(); ++i) {
            const auto& previous = bands[indices[i - 1]];
            const auto& current = bands[indices[i]];
            const double previous_center = (previous.y0 + previous.y1) / 2.0;
            const double current_center = (current.y0 + current.y1) / 2.0;
            const int pitch = static_cast<int>(std::lround(current_center - previous_center));
            if (pitch >= std::max(3, static_cast<int>(std::lround(typical_height * 0.65))) &&
                pitch <= typical_height * 3.5)
                pitches.push_back(pitch);
        }
        const int typical_pitch = median_value(std::move(pitches),
                                               static_cast<int>(std::lround(typical_height * 1.5)));

        std::vector<int> shared_cut(indices.size() > 1 ? indices.size() - 1 : 0, -1);
        for (size_t i = 0; i + 1 < indices.size(); ++i) {
            const auto& upper = bands[indices[i]];
            const auto& lower = bands[indices[i + 1]];
            const int upper_h = upper.y1 - upper.y0 + 1;
            const int lower_h = lower.y1 - lower.y0 + 1;
            const int gap_start = upper.y1 + 1;
            const int gap_end = lower.y0; // exclusive
            const int gap = gap_end - gap_start;
            const double center_distance = (lower.y0 + lower.y1 - upper.y0 - upper.y1) / 2.0;
            const int pair_scale = std::min(std::max(upper_h, lower_h),
                                            std::max(typical_height, static_cast<int>(typical_height * 1.6)));
            const bool nearby_rows = center_distance <= std::max(pair_scale * 2.2,
                                                                  typical_pitch * 1.5);
            const int max_gap = std::max(1, static_cast<int>(std::lround(
                std::min(std::max(upper_h, lower_h) * 0.65, center_distance * 0.48))));
            if (!nearby_rows || gap < 0 || gap > max_gap) continue;

            const int x0 = std::min(upper.x0, lower.x0);
            const int x1 = std::max(upper.x1, lower.x1);
            int upper_peak = band_peak_row_ink(mask, width, upper);
            int lower_peak = band_peak_row_ink(mask, width, lower);
            const int max_valley_ink = std::max(1, std::min(upper_peak, lower_peak) / 12);
            int min_ink = std::numeric_limits<int>::max();
            double best_distance = std::numeric_limits<double>::infinity();
            int valley_row = gap_start;
            const double gap_midpoint = (gap_start + gap_end) / 2.0;
            for (int y = gap_start; y < gap_end; ++y) {
                const int ink = row_ink_in_xrange(mask, width, y, x0, x1);
                const double distance = std::abs((y + 0.5) - gap_midpoint);
                if (ink < min_ink || (ink == min_ink && distance < best_distance)) {
                    min_ink = ink;
                    best_distance = distance;
                    valley_row = y;
                }
            }
            if (min_ink > max_valley_ink) continue;

            const int cut = std::clamp(static_cast<int>(std::lround(valley_row + 0.5)),
                                       gap_start, gap_end);
            shared_cut[i] = cut;
            if (debug)
                std::cerr << "debug: vertical_whitespace_cut region=" << upper.region
                          << " y=" << gap_start << ".." << gap_end
                          << " min_row_ink=" << min_ink << " cut=" << cut << "\n";
        }

        for (size_t i = 0; i < indices.size(); ++i) {
            auto& band = bands[indices[i]];
            const int original_y0 = band.y0;
            const int original_y1_exclusive = band.y1 + 1;
            const int ink_height = original_y1_exclusive - original_y0;
            const int target_pad = std::max(1, static_cast<int>(std::lround(ink_height * 0.18)));
            const int source_safety_pad = 2;

            int refined_y0 = original_y0;
            int refined_y1_exclusive = original_y1_exclusive;
            int crop_y0 = std::max(0, original_y0 - target_pad);
            int crop_y1_exclusive = std::min(height, original_y1_exclusive + target_pad);
            if (i > 0 && shared_cut[i - 1] >= 0) {
                refined_y0 = shared_cut[i - 1];
                crop_y0 = std::max(0, shared_cut[i - 1] - source_safety_pad);
            } else {
                int pad = target_pad;
                if (i > 0) {
                    const auto& previous = bands[indices[i - 1]];
                    const int available = original_y0 - previous.y1 - 1;
                    pad = std::min(pad, std::max(0, available / 2));
                }
                refined_y0 = std::max(0, original_y0 - pad);
                crop_y0 = std::max(0, original_y0 - std::max(pad, source_safety_pad));
            }
            if (i + 1 < indices.size() && shared_cut[i] >= 0) {
                refined_y1_exclusive = shared_cut[i];
                crop_y1_exclusive = std::min(height, shared_cut[i] + source_safety_pad);
            } else {
                int pad = target_pad;
                if (i + 1 < indices.size()) {
                    const auto& next = bands[indices[i + 1]];
                    const int available = next.y0 - band.y1 - 1;
                    pad = std::min(pad, std::max(0, available / 2));
                }
                refined_y1_exclusive = std::min(height, original_y1_exclusive + pad);
                crop_y1_exclusive = std::min(height, original_y1_exclusive +
                                                    std::max(pad, source_safety_pad));
            }
            band.y0 = std::clamp(refined_y0, 0, height - 1);
            band.y1 = std::clamp(refined_y1_exclusive - 1, band.y0, height - 1);
            band.crop_y0 = std::clamp(crop_y0, 0, height - 1);
            band.crop_y1 = std::clamp(crop_y1_exclusive - 1, band.crop_y0, height - 1);
            if (debug && (band.y0 != original_y0 || band.y1 + 1 != original_y1_exclusive))
                std::cerr << "debug: vertical_boundary_refine region=" << band.region
                          << " original=[" << original_y0 << "," << original_y1_exclusive
                          << ") refined=[" << band.y0 << "," << band.y1 + 1 << ")\n";
        }
    }
}

static Image crop_with_white_padding(const Image& gray, const Band& b,
                                     std::array<int, 4>* source_bbox = nullptr) {
    const int ink_y0 = b.ink_y0 >= 0 ? b.ink_y0 : b.y0;
    const int ink_y1 = b.ink_y1 >= ink_y0 ? b.ink_y1 : b.y1;
    const int line_h = ink_y1 - ink_y0 + 1;
    // Detached Ethiopic marks often sit above or below the main glyph bodies.
    const int pad_y = std::max(3, static_cast<int>(std::lround(line_h * 0.55)));
    const int pad_x = std::max(12, static_cast<int>(std::lround(line_h * 0.70)));
    const int crop_scale_height = std::max(line_h, b.y1 - b.y0 + 1);
    int x0 = std::max(0, b.x0 - pad_x);
    int x1 = std::min(gray.width - 1, b.x1 + pad_x);
    const int gutter_pad = std::max(pad_x, static_cast<int>(std::lround(crop_scale_height * 0.85)));
    const int near_gutter_edge = std::max(2, static_cast<int>(std::lround(crop_scale_height * 0.12)));
    if (b.gutter_crop_x0 >= 0 && b.region_x0 >= 0 &&
        b.x0 <= b.region_x0 + near_gutter_edge) {
        x0 = std::max(0, std::max(b.gutter_crop_x0,
                                  std::min(x0, b.x0 - gutter_pad)));
    }
    if (b.gutter_crop_x1 >= 0 && b.region_x1 > 0 &&
        b.x1 >= b.region_x1 - 1 - near_gutter_edge) {
        x1 = std::min(gray.width - 1, std::min(b.gutter_crop_x1,
                                               std::max(x1, b.x1 + gutter_pad)));
    }
    const int y0 = b.crop_y0 >= 0 ? b.crop_y0 : std::max(0, b.y0 - pad_y);
    const int y1 = b.crop_y1 >= y0 ? b.crop_y1 : std::min(gray.height - 1, b.y1 + pad_y);
    const int ow = x1 - x0 + 1, oh = y1 - y0 + 1;
    if (source_bbox) *source_bbox = {x0, y0, x1 + 1, y1 + 1};
    const int border = std::max(2, static_cast<int>(std::lround(line_h * 0.10)));
    Image out{ow + 2 * border, oh + 2 * border, std::vector<uint8_t>(static_cast<size_t>(ow + 2 * border) * (oh + 2 * border), 255)};
    for (int y = 0; y < oh; ++y)
        std::copy_n(gray.pixels.begin() + static_cast<size_t>(y0 + y) * gray.width + x0, ow,
                    out.pixels.begin() + static_cast<size_t>(y + border) * out.width + border);
    return out;
}

static void mask_crop_to_curved_region(Image& crop, const LayoutRegion& region,
                                       const std::array<int, 4>& source_bbox) {
    if (!region.curved_left && !region.curved_right) return;
    const int border_x = (crop.width - (source_bbox[2] - source_bbox[0])) / 2;
    const int border_y = (crop.height - (source_bbox[3] - source_bbox[1])) / 2;
    for (int y = source_bbox[1]; y < source_bbox[3]; ++y) {
        if (y < region.y0 || y >= region.y1) continue;
        const int left = lineforge_layout::left_at(region, y);
        const int right = lineforge_layout::right_at(region, y);
        for (int x = source_bbox[0]; x < source_bbox[2]; ++x) {
            if (x >= left && x < right) continue;
            const int cx = x - source_bbox[0] + border_x;
            const int cy = y - source_bbox[1] + border_y;
            if (cx >= 0 && cx < crop.width && cy >= 0 && cy < crop.height)
                crop.pixels[static_cast<size_t>(cy) * crop.width + cx] = 255;
        }
    }
}

static std::string json_escape(const std::string& value) {
    std::string out;
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { out.push_back('\\'); out.push_back(static_cast<char>(c)); }
        else if (c < 0x20) { out += "\\u00"; const char* hex = "0123456789abcdef"; out.push_back(hex[c >> 4]); out.push_back(hex[c & 15]); }
        else out.push_back(static_cast<char>(c));
    }
    return out;
}

struct Options {
    std::string input, output;
    std::string layout = "auto";
    std::string reading_order = "ltr";
    std::vector<std::array<int, 4>> custom_regions;
    bool deskew = true, filter_page_number = true, debug = false;
};

static std::array<int, 4> parse_region(const std::string& value) {
    std::array<int, 4> result{};
    size_t begin = 0;
    for (size_t i = 0; i < result.size(); ++i) {
        const size_t end = value.find(',', begin);
        if ((i + 1 < result.size() && end == std::string::npos) ||
            (i + 1 == result.size() && end != std::string::npos))
            throw std::runtime_error("--region must be x0,y0,x1,y1 using half-open coordinates");
        const std::string part = value.substr(begin, end == std::string::npos ? end : end - begin);
        size_t consumed = 0;
        try {
            const long parsed = std::stol(part, &consumed);
            require(consumed == part.size() && parsed >= 0 && parsed <= std::numeric_limits<int>::max(),
                    "--region coordinates must be non-negative integers");
            result[i] = static_cast<int>(parsed);
        } catch (const std::exception&) {
            throw std::runtime_error("--region coordinates must be non-negative integers");
        }
        begin = end == std::string::npos ? value.size() : end + 1;
    }
    require(result[0] < result[2] && result[1] < result[3],
            "--region must have positive width and height");
    return result;
}

static Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--version") { std::cout << "linecrop " << LINEFORGE_VERSION << "\n"; std::exit(0); }
        else if (arg == "--no-deskew") o.deskew = false;
        else if (arg == "--keep-page-numbers") o.filter_page_number = false;
        else if (arg == "--debug") o.debug = true;
        else if (arg == "--input" && i + 1 < argc) o.input = argv[++i];
        else if (arg == "--output" && i + 1 < argc) o.output = argv[++i];
        else if (arg == "--layout" && i + 1 < argc) {
            o.layout = argv[++i];
            require(o.layout == "auto" || o.layout == "single" || o.layout == "baseline",
                    "--layout must be auto, baseline, or single");
        }
        else if (arg == "--reading-order" && i + 1 < argc) {
            o.reading_order = argv[++i];
            require(o.reading_order == "ltr" || o.reading_order == "rtl", "--reading-order must be ltr or rtl");
        }
        else if (arg == "--region" && i + 1 < argc) o.custom_regions.push_back(parse_region(argv[++i]));
        else if (!arg.empty() && arg[0] != '-' && o.input.empty()) o.input = arg;
        else throw std::runtime_error("unknown or incomplete argument: " + arg);
    }
    require(!o.input.empty(), "usage: linecrop --input page.jpg --output out_dir [--layout auto|baseline|single] [--reading-order ltr|rtl] [--region x0,y0,x1,y1 ...] [--no-deskew] [--keep-page-numbers] [--debug]");
    require(!o.output.empty(), "--output DIR is required");
    return o;
}

int main(int argc, char** argv) {
    try {
        const Options opt = parse_args(argc, argv);
        const auto begin = std::chrono::steady_clock::now();
        Image original = load_image(opt.input);
        const auto loaded = std::chrono::steady_clock::now();
        require(original.width > 0 && original.height > 0, "empty image");
        require(original.pixels.size() <= MAX_PIXELS, "image exceeds the 100-megapixel safety limit");
        const Image normalized = flatten_illumination(original, opt.debug);
        auto mask = threshold_image(normalized, opt.debug);
        const auto thresholded = std::chrono::steady_clock::now();
        auto ink_count = [&]() { return static_cast<uint64_t>(std::count_if(mask.begin(), mask.end(), [](uint8_t v) { return v != 0; })); };
        if (opt.debug) std::cerr << "debug: threshold_foreground=" << ink_count() << "\n";
        suppress_page_rules(mask, original.width, original.height);
        const uint64_t after_rules = opt.debug ? ink_count() : 0;
        remove_tiny_components(mask, original.width, original.height);
        if (opt.debug) std::cerr << "debug: after_rule_suppression=" << after_rules << " after_noise_filter=" << ink_count() << "\n";
        if (opt.debug) {
            const int center_begin = original.width * 35 / 100, center_end = original.width * 65 / 100;
            int exact_begin = -1, exact_best = 0, exact_best_begin = -1;
            int low_begin = -1, low_best = 0, low_best_begin = -1;
            int exact_run = 0, low_run = 0;
            for (int x = center_begin; x < center_end; ++x) {
                uint64_t count = 0;
                for (int y = 0; y < original.height; ++y)
                    count += mask[static_cast<size_t>(y) * original.width + x] != 0;
                if (count == 0) {
                    if (!exact_run) exact_begin = x;
                    ++exact_run;
                    if (exact_run > exact_best) { exact_best = exact_run; exact_best_begin = exact_begin; }
                }
                else exact_run = 0;
                if (count <= static_cast<uint64_t>(original.height / 100)) {
                    if (!low_run) low_begin = x;
                    ++low_run;
                    if (low_run > low_best) { low_best = low_run; low_best_begin = low_begin; }
                }
                else low_run = 0;
            }
            const LayoutRegion whole{0, 0, original.width, original.height};
            const int left_runs = exact_best_begin >= 0
                ? lineforge_layout::active_row_runs(mask, original.width, whole, 0, exact_best_begin) : 0;
            const int right_runs = exact_best_begin >= 0
                ? lineforge_layout::active_row_runs(mask, original.width, whole, exact_best_begin + exact_best, original.width) : 0;
            std::cerr << "debug: center_exact_gap=[" << exact_best_begin << "," << exact_best_begin + exact_best
                      << ") width=" << exact_best << " low_ink_gap=[" << low_best_begin << "," << low_best_begin + low_best
                      << ") width=" << low_best << " active_runs=" << left_runs << "," << right_runs
                      << " threshold_per_column=" << original.height / 100 << "\n";
        }
        bool preserve_vertical_gutter = false;
        lineforge_layout::Cut pre_deskew_cut;
        if (opt.deskew && (opt.layout == "auto" || opt.layout == "baseline") && opt.custom_regions.empty()) {
            pre_deskew_cut = lineforge_layout::find_cut(
                mask, original.width, LayoutRegion{0, 0, original.width, original.height}, original.height,
                opt.layout == "auto");
            preserve_vertical_gutter = pre_deskew_cut.found && pre_deskew_cut.vertical;
        }
        const double angle = opt.deskew && !preserve_vertical_gutter
            ? estimate_deskew(mask, original.width, original.height) : 0.0;
        const auto deskewed = std::chrono::steady_clock::now();
        Image processed = rotate_gray(original, angle);
        if (std::abs(angle) >= 1e-8) mask = rotate_mask(mask, original.width, original.height, angle);

        if (opt.debug && (opt.layout == "auto" || opt.layout == "baseline") && opt.custom_regions.empty()) {
            const auto cut = lineforge_layout::find_cut(
                mask, processed.width, LayoutRegion{0, 0, processed.width, processed.height}, processed.height,
                opt.layout == "auto");
            std::cerr << "debug: gutter_pre_deskew="
                      << (pre_deskew_cut.found ? (pre_deskew_cut.vertical ? "vertical" : "horizontal") : "none")
                      << "[" << pre_deskew_cut.begin << "," << pre_deskew_cut.end << "] post_deskew="
                      << (cut.found ? (cut.vertical ? "vertical" : "horizontal") : "none")
                      << "[" << cut.begin << "," << cut.end << "]\n";
        }

        std::vector<LayoutRegion> regions;
        std::string layout_name = opt.custom_regions.empty() ? opt.layout : "custom";
        const std::string reading_order_name = opt.custom_regions.empty() ? opt.reading_order : "explicit";
        if (!opt.custom_regions.empty()) {
            for (const auto& r : opt.custom_regions) {
                require(r[0] < r[2] && r[1] < r[3] && r[2] <= processed.width && r[3] <= processed.height,
                        "custom --region is outside the deskewed image bounds");
                regions.push_back(LayoutRegion{r[0], r[1], r[2], r[3]});
            }
            for (size_t i = 0; i < regions.size(); ++i) for (size_t j = i + 1; j < regions.size(); ++j) {
                const bool overlap = regions[i].x0 < regions[j].x1 && regions[j].x0 < regions[i].x1 &&
                                     regions[i].y0 < regions[j].y1 && regions[j].y0 < regions[i].y1;
                require(!overlap, "custom --region rectangles must not overlap");
            }
        } else if (opt.layout == "single") {
            regions.push_back(LayoutRegion{0, 0, processed.width, processed.height});
        } else {
            regions = lineforge_layout::detect_regions(mask, processed.width, processed.height,
                                                       opt.reading_order == "rtl",
                                                       opt.layout == "auto");
        }

        const auto crop_extents = find_gutter_crop_extents(
            regions, processed.width, opt.custom_regions.empty() &&
                (opt.layout == "auto" || opt.layout == "baseline"));

        std::vector<Band> bands;
        std::vector<size_t> lines_per_region(regions.size(), 0);
        for (size_t region_index = 0; region_index < regions.size(); ++region_index) {
            const auto& r = regions[region_index];
            const int rw = r.x1 - r.x0, rh = r.y1 - r.y0;
            if (rw <= 0 || rh <= 0) continue;
            std::vector<uint8_t> local_mask(static_cast<size_t>(rw) * rh, 0);
            std::vector<uint8_t> local_gray(static_cast<size_t>(rw) * rh, 255);
            for (int y = 0; y < rh; ++y) {
                const int page_y = r.y0 + y;
                const int row_left = lineforge_layout::left_at(r, page_y);
                const int row_right = lineforge_layout::right_at(r, page_y);
                const int copy_width = std::max(0, row_right - row_left);
                const auto source = mask.begin() + static_cast<size_t>(page_y) * processed.width + row_left;
                const auto source_gray = processed.pixels.begin() + static_cast<size_t>(page_y) * processed.width + row_left;
                std::copy_n(source, copy_width,
                    local_mask.begin() + static_cast<size_t>(y) * rw + (row_left - r.x0));
                std::copy_n(source_gray, copy_width,
                    local_gray.begin() + static_cast<size_t>(y) * rw + (row_left - r.x0));
            }
            auto local_bands = find_bands(local_mask, rw, rh, opt.debug);
            recover_shallow_bands(local_bands, local_mask, local_gray, rw, rh, opt.debug);
            for (auto& b : local_bands) {
                b.x0 += r.x0; b.x1 += r.x0;
                b.y0 += r.y0; b.y1 += r.y0;
                b.region = static_cast<int>(region_index + 1);
                b.region_x0 = r.x0;
                b.region_x1 = r.x1;
                b.gutter_crop_x0 = crop_extents[region_index].gutter_left_x;
                b.gutter_crop_x1 = crop_extents[region_index].gutter_right_x;
                if (r.curved_left || r.curved_right) {
                    const int first = std::clamp(b.y0 - r.y0, 0, rh - 1);
                    const int last = std::clamp(b.y1 - r.y0, first, rh - 1);
                    if (r.curved_left) {
                        int seam = std::numeric_limits<int>::max();
                        for (int y = first; y <= last; ++y)
                            seam = std::min(seam, r.left_by_row[static_cast<size_t>(y)]);
                        b.region_x0 = r.left_by_row[static_cast<size_t>((first + last) / 2)];
                        b.gutter_crop_x0 = seam;
                    }
                    if (r.curved_right) {
                        int seam = std::numeric_limits<int>::min();
                        for (int y = first; y <= last; ++y)
                            seam = std::max(seam, r.right_by_row[static_cast<size_t>(y)]);
                        b.region_x1 = r.right_by_row[static_cast<size_t>((first + last) / 2)];
                        b.gutter_crop_x1 = seam - 1;
                    }
                    if (r.seam_confidence < 0.98)
                        b.review_reasons.emplace_back("curved_layout_boundary_uncertainty");
                }
            }
            lines_per_region[region_index] = local_bands.size();
            bands.insert(bands.end(), local_bands.begin(), local_bands.end());
        }
        if (opt.custom_regions.empty() && opt.layout == "auto")
            mark_likely_nontext_fragments(bands, processed.width, processed.height);
        merge_short_line_fragments(bands, processed.height);
        std::fill(lines_per_region.begin(), lines_per_region.end(), 0);
        for (const auto& b : bands)
            if (b.region > 0 && static_cast<size_t>(b.region) <= lines_per_region.size())
                ++lines_per_region[static_cast<size_t>(b.region - 1)];
        mark_likely_page_numbers(bands, processed.width, processed.height, opt.filter_page_number);
        mark_uncertain_bands(bands, processed.width, processed.height);
        // Classification and exclusions are final before this purely geometric
        // pass, so the line-candidate decisions remain unchanged.
        refine_vertical_boundaries(bands, mask, processed.width, processed.height, opt.debug);
        const auto segmented = std::chrono::steady_clock::now();
        if (opt.debug) {
            std::cerr << "debug: deskew_degrees=" << angle << " layout=" << layout_name
                      << " reading_order=" << reading_order_name << " regions=" << regions.size()
                      << " detected_bands=" << bands.size()
                      << " preserve_vertical_gutter=" << (preserve_vertical_gutter ? "true" : "false") << "\n";
            for (size_t i = 0; i < regions.size(); ++i) {
                const auto& r = regions[i];
                std::cerr << "debug: region=" << i + 1 << " bbox=[" << r.x0 << "," << r.y0 << ","
                          << r.x1 << "," << r.y1 << "] lines=" << lines_per_region[i] << "\n";
                if (crop_extents[i].gutter_left_x >= 0 || crop_extents[i].gutter_right_x >= 0)
                    std::cerr << "debug: gutter_crop_extent region=" << i + 1
                              << " left=" << crop_extents[i].gutter_left_x
                              << " right=" << crop_extents[i].gutter_right_x << "\n";
            }
        }
        fs::create_directories(opt.output);
        std::ofstream manifest(fs::path(opt.output) / "manifest.json");
        require(manifest.good(), "cannot create manifest in output directory");
        manifest << "{\n  \"engine\": \"linecrop\",\n  \"version\": \"" << LINEFORGE_VERSION << "\",\n";
        manifest << "  \"input\": \"" << json_escape(fs::path(opt.input).filename().string()) << "\",\n";
        manifest << "  \"image_size\": {\"width\": " << processed.width << ", \"height\": " << processed.height << "},\n";
        manifest << "  \"deskew_correction_degrees\": " << std::fixed << std::setprecision(3) << angle << ",\n";
        manifest << "  \"layout\": {\"mode\": \"" << layout_name << "\", \"reading_order\": \""
                 << reading_order_name << "\", \"hierarchy\": \"page>layout_region>line\", \"regions\": [";
        for (size_t i = 0; i < regions.size(); ++i) {
            const auto& r = regions[i];
            if (i) manifest << ", ";
            manifest << "{\"id\": " << i + 1 << ", \"bbox_xyxy\": [" << r.x0 << ", " << r.y0
                     << ", " << r.x1 << ", " << r.y1 << "], \"parent_id\": 0, \"reading_order_index\": "
                     << i + 1 << ", \"next_region_id\": ";
            if (i + 1 < regions.size()) manifest << i + 2;
            else manifest << "null";
            manifest << ", \"geometry\": \""
                     << ((r.curved_left || r.curved_right) ? "piecewise_vertical_boundary" : "axis_aligned")
                     << "\", \"detected_lines\": " << lines_per_region[i];
            const int sample_step = std::max(1, (r.y1 - r.y0) / 32);
            for (const bool left_edge : {true, false}) {
                const bool curved_edge = left_edge ? r.curved_left : r.curved_right;
                if (!curved_edge) continue;
                manifest << (left_edge ? ", \"left_edge_xy\": [" : ", \"right_edge_xy\": [");
                bool first_point = true;
                for (int y = r.y0; y < r.y1; y += sample_step) {
                    if (!first_point) manifest << ", ";
                    first_point = false;
                    manifest << "[" << (left_edge ? lineforge_layout::left_at(r, y)
                                                  : lineforge_layout::right_at(r, y))
                             << ", " << y << "]";
                }
                const int last_y = r.y1 - 1;
                if (last_y >= r.y0 && (last_y - r.y0) % sample_step != 0) {
                    if (!first_point) manifest << ", ";
                    manifest << "[" << (left_edge ? lineforge_layout::left_at(r, last_y)
                                                  : lineforge_layout::right_at(r, last_y))
                             << ", " << last_y << "]";
                }
                manifest << "]";
            }
            manifest << "}";
        }
        manifest << "], \"fallback\": \"--layout baseline disables curved-seam search\"},\n"
                 << "  \"page_number_filter\": \"conservative geometric heuristic; manually review\",\n"
                 << "  \"lines\": [\n";
        size_t kept = 0, skipped = 0;
        for (const auto& b : bands) {
            if (b.page_number || b.non_text_fragment) { ++skipped; continue; }
            const std::string name = "line_" + (kept + 1 < 10 ? std::string("000") : kept + 1 < 100 ? std::string("00") : kept + 1 < 1000 ? std::string("0") : std::string()) + std::to_string(kept + 1) + ".png";
            std::array<int, 4> source_crop_bbox{};
            Image crop = crop_with_white_padding(processed, b, &source_crop_bbox);
            if (b.region > 0 && static_cast<size_t>(b.region) <= regions.size())
                mask_crop_to_curved_region(crop, regions[static_cast<size_t>(b.region - 1)], source_crop_bbox);
            write_png_gray(fs::path(opt.output) / name, crop);
            if (kept) manifest << ",\n";
            manifest << "    {\"file\": \"" << name << "\", \"bbox_xyxy\": [" << b.x0 << ", " << b.y0 << ", " << b.x1 + 1 << ", " << b.y1 + 1
                     << "], \"source_crop_bbox_xyxy\": [" << source_crop_bbox[0] << ", " << source_crop_bbox[1]
                     << ", " << source_crop_bbox[2] << ", " << source_crop_bbox[3] << "], \"crop_size\": ["
                     << crop.width << ", " << crop.height << "], \"ink_pixels\": " << b.ink
                     << ", \"region\": " << b.region << ", \"parent_region_id\": " << b.region
                     << ", \"reading_order_index\": " << kept + 1
                     << ", \"relationship\": \"contained_by_region\", \"review_required\": "
                     << (b.review_reasons.empty() ? "false" : "true") << ", \"review_reasons\": [";
            for (size_t i = 0; i < b.review_reasons.size(); ++i) {
                if (i) manifest << ", ";
                manifest << "\"" << b.review_reasons[i] << "\"";
            }
            manifest << "]}";
            ++kept;
        }
        manifest << "\n  ],\n  \"excluded\": [";
        bool first = true;
        for (const auto& b : bands) if (b.page_number || b.non_text_fragment) {
            if (!first) manifest << ", ";
            first = false;
            const char* reason = b.page_number ? "likely_centered_page_number" : "likely_non_text_fragment";
            manifest << "{\"bbox_xyxy\": [" << b.x0 << ", " << b.y0 << ", " << b.x1 + 1 << ", " << b.y1 + 1
                     << "], \"reason\": \"" << reason << "\", \"review\": true, \"review_required\": true, \"region\": " << b.region << "}";
        }
        const auto report_time = std::chrono::steady_clock::now();
        const auto elapsed_ms = [](auto a, auto b) {
            return std::chrono::duration<double, std::milli>(b - a).count();
        };
        manifest << "],\n  \"timing_ms\": {\"decode\": " << elapsed_ms(begin, loaded)
                 << ", \"threshold\": " << elapsed_ms(loaded, thresholded)
                 << ", \"cleanup_and_deskew\": " << elapsed_ms(thresholded, deskewed)
                 << ", \"segmentation\": " << elapsed_ms(deskewed, segmented)
                 << ", \"output\": " << elapsed_ms(segmented, report_time)
                 << ", \"total_before_manifest_finalize\": " << elapsed_ms(begin, report_time)
                 << "},\n  \"detected_bands\": " << bands.size() << "\n}\n";
        manifest.close();
        require(manifest.good(), "failed while writing manifest");
        const auto end = std::chrono::steady_clock::now();
        if (opt.debug) {
            const auto ms = [](auto a, auto b) { return std::chrono::duration<double, std::milli>(b - a).count(); };
            std::cerr << "debug: stage_ms decode=" << ms(begin, loaded) << " threshold=" << ms(loaded, thresholded)
                      << " cleanup_and_deskew=" << ms(thresholded, deskewed) << " bands=" << ms(deskewed, segmented)
                      << " output=" << ms(segmented, end) << "\n";
        }
        const double ms = std::chrono::duration<double, std::milli>(end - begin).count();
        std::cout << "linecrop " << LINEFORGE_VERSION << " lines=" << kept << " excluded_candidates=" << skipped << " deskew_degrees=" << angle
                  << " elapsed_ms=" << std::fixed << std::setprecision(2) << ms << " output=" << opt.output << "\n";
        return kept ? 0 : 2;
    } catch (const std::exception& e) {
        std::cerr << "linecrop: " << e.what() << "\n";
        return 1;
    }
}
