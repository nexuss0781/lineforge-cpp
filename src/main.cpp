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
};

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
    std::vector<uint8_t> rule_column(width, 0);
    for (int x = 0; x < width; ++x) {
        int run = 0, longest = 0, count = 0;
        for (int y = 0; y < height; ++y) {
            if (mask[static_cast<size_t>(y) * width + x]) { ++run; ++count; longest = std::max(longest, run); }
            else run = 0;
        }
        const bool near_outer_area = x < width * 0.25 || x > width * 0.75;
        if (near_outer_area && longest >= height * 0.40 && count >= height * 0.45)
            rule_column[x] = 1;
    }
    const int padding = std::max(2, std::min(16, static_cast<int>(std::lround(width * 0.01))));
    for (int x = 0; x < width; ++x) {
        if (!rule_column[x]) continue;
        for (int xx = std::max(0, x - padding); xx <= std::min(width - 1, x + padding); ++xx)
            for (int y = 0; y < height; ++y) mask[static_cast<size_t>(y) * width + xx] = 0;
    }
}

static void remove_tiny_components(std::vector<uint8_t>& mask, int width, int height) {
    std::vector<uint8_t> visited(mask.size(), 0);
    std::vector<uint32_t> component;
    component.reserve(32);
    for (int y = 0; y < height; ++y) for (int x = 0; x < width; ++x) {
        const size_t start = static_cast<size_t>(y) * width + x;
        if (mask[start] != 1 || visited[start]) continue;
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
                if (mask[next] == 1 && !visited[next]) { visited[next] = 1; component.push_back(static_cast<uint32_t>(next)); }
            }
        }
        if (component.size() <= 2) for (uint32_t pos : component) mask[pos] = 0;
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

static std::vector<Band> find_bands(const std::vector<uint8_t>& mask, int w, int h) {
    std::vector<uint32_t> rows(h, 0);
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x) rows[y] += mask[static_cast<size_t>(y) * w + x] != 0;
    std::vector<int> smooth(h, 0);
    for (int y = 0; y < h; ++y) {
        for (int dy = -1; dy <= 1; ++dy) if (y + dy >= 0 && y + dy < h) smooth[y] += rows[y + dy];
    }
    const int row_threshold = std::max(3, static_cast<int>(std::ceil(w * 0.004)));
    std::vector<std::pair<int, int>> raw;
    for (int y = 0; y < h;) {
        if (smooth[y] < row_threshold) { ++y; continue; }
        const int start = y;
        while (y + 1 < h && smooth[y + 1] >= row_threshold) ++y;
        raw.emplace_back(start, y);
        ++y;
    }
    std::vector<int> heights;
    for (auto [a, b] : raw) if (b - a + 1 >= 3) heights.push_back(b - a + 1);
    int typical = 12;
    if (!heights.empty()) {
        std::sort(heights.begin(), heights.end());
        typical = heights[heights.size() / 2];
    }
    const int merge_gap = std::clamp(static_cast<int>(std::lround(typical * 0.24)), 2, 14);
    std::vector<std::pair<int, int>> merged;
    for (auto run : raw) {
        if (!merged.empty() && run.first - merged.back().second - 1 <= merge_gap) merged.back().second = run.second;
        else merged.push_back(run);
    }
    std::vector<Band> bands;
    for (auto [y0, y1] : merged) {
        const int bh = y1 - y0 + 1;
        if (bh < 2) continue;
        int x0 = w, x1 = -1;
        uint64_t ink = 0;
        std::vector<uint32_t> columns(w, 0);
        for (int y = y0; y <= y1; ++y) for (int x = 0; x < w; ++x) {
            if (mask[static_cast<size_t>(y) * w + x]) { ++columns[x]; ++ink; }
        }
        const int x_threshold = std::max(1, static_cast<int>(std::ceil(bh * 0.025)));
        for (int x = 0; x < w; ++x) if (columns[x] >= static_cast<uint32_t>(x_threshold)) { x0 = std::min(x0, x); x1 = std::max(x1, x); }
        if (x1 < x0 || ink < std::max<uint64_t>(8, static_cast<uint64_t>(w * 0.003))) continue;
        const int ncomp = component_count(mask, w, x0, y0, x1, y1);
        if (ncomp == 0) continue;
        bands.push_back(Band{y0, y1, x0, x1, ink, ncomp, false});
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
    const size_t last = bands.size() - 1;
    for (size_t i = 0; i < bands.size(); ++i) {
        const auto& b = bands[i];
        const double bw = b.x1 - b.x0 + 1, bh = b.y1 - b.y0 + 1;
        const double center = (b.x0 + b.x1) / 2.0;
        const bool compact = bw <= w * 0.16 && bh <= median_h * 1.28 && b.components <= 8;
        const bool centered = std::abs(center - w / 2.0) <= w * 0.21;
        const bool lower_half = b.y0 >= h * 0.40;
        if (i == last && compact && centered && lower_half) bands[i].page_number = true;
    }
}

static Image crop_with_white_padding(const Image& gray, const Band& b) {
    const int line_h = b.y1 - b.y0 + 1;
    const int pad_y = std::max(2, static_cast<int>(std::lround(line_h * 0.14)));
    const int pad_x = std::max(4, static_cast<int>(std::lround(line_h * 0.18)));
    const int x0 = std::max(0, b.x0 - pad_x), x1 = std::min(gray.width - 1, b.x1 + pad_x);
    const int y0 = std::max(0, b.y0 - pad_y), y1 = std::min(gray.height - 1, b.y1 + pad_y);
    const int ow = x1 - x0 + 1, oh = y1 - y0 + 1;
    const int border = std::max(2, static_cast<int>(std::lround(line_h * 0.10)));
    Image out{ow + 2 * border, oh + 2 * border, std::vector<uint8_t>(static_cast<size_t>(ow + 2 * border) * (oh + 2 * border), 255)};
    for (int y = 0; y < oh; ++y)
        std::copy_n(gray.pixels.begin() + static_cast<size_t>(y0 + y) * gray.width + x0, ow,
                    out.pixels.begin() + static_cast<size_t>(y + border) * out.width + border);
    return out;
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

struct Options { std::string input, output; bool deskew = true, filter_page_number = true, debug = false; };

static Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--version") { std::cout << "linecrop 0.1.0\n"; std::exit(0); }
        else if (arg == "--no-deskew") o.deskew = false;
        else if (arg == "--keep-page-numbers") o.filter_page_number = false;
        else if (arg == "--debug") o.debug = true;
        else if (arg == "--input" && i + 1 < argc) o.input = argv[++i];
        else if (arg == "--output" && i + 1 < argc) o.output = argv[++i];
        else if (!arg.empty() && arg[0] != '-' && o.input.empty()) o.input = arg;
        else throw std::runtime_error("unknown or incomplete argument: " + arg);
    }
    require(!o.input.empty(), "usage: linecrop --input page.jpg --output out_dir [--no-deskew] [--keep-page-numbers] [--debug]");
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
        auto mask = threshold_image(original, opt.debug);
        const auto thresholded = std::chrono::steady_clock::now();
        auto ink_count = [&]() { return static_cast<uint64_t>(std::count(mask.begin(), mask.end(), static_cast<uint8_t>(1))); };
        if (opt.debug) std::cerr << "debug: threshold_foreground=" << ink_count() << "\n";
        suppress_page_rules(mask, original.width, original.height);
        const uint64_t after_rules = opt.debug ? ink_count() : 0;
        remove_tiny_components(mask, original.width, original.height);
        if (opt.debug) std::cerr << "debug: after_rule_suppression=" << after_rules << " after_noise_filter=" << ink_count() << "\n";
        const double angle = opt.deskew ? estimate_deskew(mask, original.width, original.height) : 0.0;
        const auto deskewed = std::chrono::steady_clock::now();
        Image processed = rotate_gray(original, angle);
        if (std::abs(angle) >= 1e-8) mask = rotate_mask(mask, original.width, original.height, angle);
        auto bands = find_bands(mask, processed.width, processed.height);
        mark_likely_page_numbers(bands, processed.width, processed.height, opt.filter_page_number);
        const auto segmented = std::chrono::steady_clock::now();
        if (opt.debug) std::cerr << "debug: deskew_degrees=" << angle << " detected_bands=" << bands.size() << "\n";
        fs::create_directories(opt.output);
        std::ofstream manifest(fs::path(opt.output) / "manifest.json");
        require(manifest.good(), "cannot create manifest in output directory");
        manifest << "{\n  \"engine\": \"linecrop\",\n  \"version\": \"0.1.0\",\n";
        manifest << "  \"input\": \"" << json_escape(fs::path(opt.input).filename().string()) << "\",\n";
        manifest << "  \"image_size\": {\"width\": " << processed.width << ", \"height\": " << processed.height << "},\n";
        manifest << "  \"deskew_correction_degrees\": " << std::fixed << std::setprecision(3) << angle << ",\n";
        manifest << "  \"page_number_filter\": \"conservative geometric heuristic; manually review\",\n  \"lines\": [\n";
        size_t kept = 0, skipped = 0;
        for (const auto& b : bands) {
            if (b.page_number) { ++skipped; continue; }
            const std::string name = "line_" + (kept + 1 < 10 ? std::string("000") : kept + 1 < 100 ? std::string("00") : kept + 1 < 1000 ? std::string("0") : std::string()) + std::to_string(kept + 1) + ".png";
            Image crop = crop_with_white_padding(processed, b);
            write_png_gray(fs::path(opt.output) / name, crop);
            if (kept) manifest << ",\n";
            manifest << "    {\"file\": \"" << name << "\", \"bbox_xyxy\": [" << b.x0 << ", " << b.y0 << ", " << b.x1 + 1 << ", " << b.y1 + 1
                     << "], \"crop_size\": [" << crop.width << ", " << crop.height << "], \"ink_pixels\": " << b.ink << "}";
            ++kept;
        }
        manifest << "\n  ],\n  \"excluded\": [";
        bool first = true;
        for (const auto& b : bands) if (b.page_number) {
            if (!first) manifest << ", ";
            first = false;
            manifest << "{\"bbox_xyxy\": [" << b.x0 << ", " << b.y0 << ", " << b.x1 + 1 << ", " << b.y1 + 1 << "], \"reason\": \"likely_centered_page_number\", \"review\": true}";
        }
        manifest << "],\n  \"detected_bands\": " << bands.size() << "\n}\n";
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
        std::cout << "lines=" << kept << " excluded_candidates=" << skipped << " deskew_degrees=" << angle
                  << " elapsed_ms=" << std::fixed << std::setprecision(2) << ms << " output=" << opt.output << "\n";
        return kept ? 0 : 2;
    } catch (const std::exception& e) {
        std::cerr << "linecrop: " << e.what() << "\n";
        return 1;
    }
}
