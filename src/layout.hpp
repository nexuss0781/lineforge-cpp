#pragma once
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <numeric>
#include <vector>

struct LayoutRegion {
    LayoutRegion() = default;
    LayoutRegion(int left, int top, int right, int bottom)
        : x0(left), y0(top), x1(right), y1(bottom) {}
    int x0 = 0;
    int y0 = 0;
    int x1 = 0; // exclusive
    int y1 = 0; // exclusive
    // Optional row-wise bounds encode a vertically swept, non-rectangular block.
    // Empty vectors mean the corresponding bound is the constant x0/x1.
    std::vector<int> left_by_row;
    std::vector<int> right_by_row;
    bool curved_left = false;
    bool curved_right = false;
    double seam_confidence = 1.0;
};

namespace lineforge_layout {
struct Cut {
    bool found = false;
    bool vertical = false;
    int begin = 0;
    int end = 0;
    int width = 0;
    double confidence = 0.0;
    // For a curved vertical cut, one x coordinate per source row in the region.
    std::vector<int> path;
};

inline int left_at(const LayoutRegion& r, int y) {
    if (r.left_by_row.empty()) return r.x0;
    return r.left_by_row[static_cast<size_t>(std::clamp(y - r.y0, 0, r.y1 - r.y0 - 1))];
}
inline int right_at(const LayoutRegion& r, int y) {
    if (r.right_by_row.empty()) return r.x1;
    return r.right_by_row[static_cast<size_t>(std::clamp(y - r.y0, 0, r.y1 - r.y0 - 1))];
}
inline uint64_t count_ink(const std::vector<uint8_t>& mask, int image_width,
                          const LayoutRegion& r) {
    uint64_t total = 0;
    for (int y = r.y0; y < r.y1; ++y) {
        const size_t row = static_cast<size_t>(y) * image_width;
        for (int x = left_at(r, y); x < right_at(r, y); ++x)
            total += mask[row + static_cast<size_t>(x)] != 0;
    }
    return total;
}
inline int active_row_runs(const std::vector<uint8_t>& mask, int image_width,
                           const LayoutRegion& r, int x0, int x1) {
    int runs = 0;
    bool active = false;
    for (int y = r.y0; y < r.y1; ++y) {
        bool row_active = false;
        const size_t row = static_cast<size_t>(y) * image_width;
        const int begin = std::max(x0, left_at(r, y));
        const int end = std::min(x1, right_at(r, y));
        for (int x = begin; x < end; ++x) {
            if (mask[row + static_cast<size_t>(x)]) { row_active = true; break; }
        }
        if (row_active && !active) ++runs;
        active = row_active;
    }
    return runs;
}
inline Cut widest_empty_cut(const std::vector<uint64_t>& projection,
                            int start, int end, int min_width,
                            bool vertical, uint64_t total_ink,
                            uint64_t max_occupancy = 0,
                            int search_start = -1, int search_end = -1) {
    Cut best;
    const int side_guard = std::max(2, (end - start) / 100);
    const int scan_start = std::clamp(search_start < 0 ? start : search_start, start, end);
    const int scan_end = std::clamp(search_end < 0 ? end : search_end, scan_start, end);
    std::vector<uint64_t> prefix(projection.size() + 1, 0);
    for (size_t i = 0; i < projection.size(); ++i)
        prefix[i + 1] = prefix[i] + projection[i];
    for (int i = scan_start + side_guard; i < scan_end - side_guard;) {
        if (projection[static_cast<size_t>(i)] > max_occupancy) { ++i; continue; }
        const int gap_begin = i;
        while (i < scan_end - side_guard && projection[static_cast<size_t>(i)] <= max_occupancy) ++i;
        const int gap_end = i;
        const int gap_width = gap_end - gap_begin;
        if (gap_width < min_width || gap_begin <= start || gap_end >= end) continue;
        const uint64_t before = prefix[static_cast<size_t>(gap_begin)] - prefix[static_cast<size_t>(start)];
        const uint64_t after = prefix[static_cast<size_t>(end)] - prefix[static_cast<size_t>(gap_end)];
        const uint64_t min_side = std::max<uint64_t>(8, total_ink / 100);
        if (before < min_side || after < min_side) continue;
        if (!best.found || gap_width > best.width)
            best = Cut{true, vertical, gap_begin, gap_end, gap_width, 1.0, {}};
    }
    return best;
}

inline Cut curved_vertical_seam(const std::vector<uint8_t>& mask, int width,
                                const LayoutRegion& r, uint64_t total_ink) {
    const int rw = r.x1 - r.x0, rh = r.y1 - r.y0;
    if (rw < 160 || rh < 100 || total_ink < 128) return {};
    const int scale = std::max(1, (std::max(rw, rh) + 511) / 512);
    const int nx = (rw + scale - 1) / scale;
    const int ny = (rh + scale - 1) / scale;
    if (nx < 40 || ny < 32) return {};
    // Curved cuts are for blocks/columns, never narrow word groups or margins.
    const int guard = std::max(1, nx * 25 / 100);
    const int min_x = guard, max_x = nx - guard - 1;
    if (max_x <= min_x) return {};

    // Build a coarse occupancy field in one pass over the block. This bounds
    // path-search cost independently of source resolution.
    std::vector<uint32_t> ink(static_cast<size_t>(nx) * ny, 0);
    std::vector<uint32_t> area(static_cast<size_t>(nx) * ny, 0);
    for (int y = r.y0; y < r.y1; ++y) {
        const int gy = (y - r.y0) / scale;
        const int left = left_at(r, y), right = right_at(r, y);
        const size_t row = static_cast<size_t>(y) * width;
        for (int x = left; x < right; ++x) {
            const int gx = (x - r.x0) / scale;
            const size_t cell = static_cast<size_t>(gy) * nx + gx;
            ++area[cell];
            ink[cell] += mask[row + static_cast<size_t>(x)] != 0;
        }
    }
    const size_t cells = static_cast<size_t>(nx) * ny;
    std::vector<float> density(cells, 1.0f);
    for (size_t i = 0; i < cells; ++i)
        if (area[i]) density[i] = static_cast<float>(ink[i]) / static_cast<float>(area[i]);
    const int seam_radius = std::max(2, nx / 64);
    std::vector<float> seam_density(cells, 1.0f);
    for (int y = 0; y < ny; ++y) for (int x = min_x; x <= max_x; ++x) {
        float sum = 0.0f;
        int count = 0;
        for (int xx = std::max(0, x - seam_radius); xx <= std::min(nx - 1, x + seam_radius); ++xx) {
            sum += density[static_cast<size_t>(y) * nx + xx];
            ++count;
        }
        seam_density[static_cast<size_t>(y) * nx + x] =
            sum / static_cast<float>(std::max(1, count));
    }

    // Dynamic programming permits a gently bending seam. Movement is penalized
    // so word spaces cannot masquerade as structural column boundaries.
    const int max_step = 2;
    const float step_penalty = 0.018f;
    std::vector<float> cost(cells, std::numeric_limits<float>::infinity());
    std::vector<int16_t> parent(cells, -1);
    for (int x = min_x; x <= max_x; ++x)
        cost[static_cast<size_t>(x)] = seam_density[static_cast<size_t>(x)];
    for (int y = 1; y < ny; ++y) {
        for (int x = min_x; x <= max_x; ++x) {
            float best = std::numeric_limits<float>::infinity();
            int best_parent = -1;
            for (int dx = -max_step; dx <= max_step; ++dx) {
                const int px = x - dx;
                if (px < min_x || px > max_x) continue;
                const float candidate = cost[static_cast<size_t>(y - 1) * nx + px] +
                    step_penalty * static_cast<float>(std::abs(dx)) +
                    (std::abs(dx) == max_step ? 0.004f : 0.0f);
                if (candidate < best) { best = candidate; best_parent = px; }
            }
            const size_t i = static_cast<size_t>(y) * nx + x;
            cost[i] = best + seam_density[i];
            parent[i] = static_cast<int16_t>(best_parent);
        }
    }
    int end_x = min_x;
    float best_cost = std::numeric_limits<float>::infinity();
    for (int x = min_x; x <= max_x; ++x) {
        const float candidate = cost[static_cast<size_t>(ny - 1) * nx + x];
        if (candidate < best_cost) { best_cost = candidate; end_x = x; }
    }
    std::vector<int> coarse(static_cast<size_t>(ny));
    coarse.back() = end_x;
    for (int y = ny - 1; y > 0; --y) {
        const int p = parent[static_cast<size_t>(y) * nx + coarse[static_cast<size_t>(y)]];
        if (p < 0) return {};
        coarse[static_cast<size_t>(y - 1)] = p;
    }
    const auto [path_min, path_max] = std::minmax_element(coarse.begin(), coarse.end());
    int coarse_variation = 0;
    for (size_t i = 1; i < coarse.size(); ++i)
        coarse_variation += std::abs(coarse[i] - coarse[i - 1]);
    if (*path_max - *path_min > std::max(3, nx / 5) ||
        coarse_variation > ny / 3 + nx / 10)
        return {};

    Cut result;
    result.found = true;
    result.vertical = true;
    result.path.resize(static_cast<size_t>(rh));
    const int search_radius = std::max(2, scale * 2);
    int clear_rows = 0, active_path_rows = 0;
    std::vector<int> clear_widths;
    clear_widths.reserve(static_cast<size_t>(rh));
    for (int y = r.y0; y < r.y1; ++y) {
        const double gy = (y - r.y0 + 0.5) / scale - 0.5;
        const int g0 = std::clamp(static_cast<int>(std::floor(gy)), 0, ny - 1);
        const int g1 = std::min(ny - 1, g0 + 1);
        const double fraction = std::clamp(gy - g0, 0.0, 1.0);
        const double gx = coarse[static_cast<size_t>(g0)] * (1.0 - fraction) +
                          coarse[static_cast<size_t>(g1)] * fraction;
        const int expected = std::clamp(r.x0 + static_cast<int>(std::lround((gx + 0.5) * scale)),
                                        left_at(r, y) + 1, right_at(r, y) - 2);
        const size_t row = static_cast<size_t>(y) * width;
        int best_x = -1, best_local = std::numeric_limits<int>::max();
        for (int x = std::max(left_at(r, y) + 1, expected - search_radius);
             x <= std::min(right_at(r, y) - 2, expected + search_radius); ++x) {
            // Avoid cutting thresholded glyph pixels; prefer a stable blank channel.
            if (mask[row + static_cast<size_t>(x)]) continue;
            int local = 0;
            for (int yy = std::max(r.y0, y - 1); yy <= std::min(r.y1 - 1, y + 1); ++yy) {
                const size_t rr = static_cast<size_t>(yy) * width;
                for (int xx = std::max(left_at(r, yy), x - 1);
                     xx <= std::min(right_at(r, yy) - 1, x + 1); ++xx)
                    local += mask[rr + static_cast<size_t>(xx)] != 0;
            }
            if (local > 2) continue;
            if (local < best_local || (local == best_local &&
                (best_x < 0 || std::abs(x - expected) < std::abs(best_x - expected)))) {
                best_local = local;
                best_x = x;
            }
        }
        if (best_x < 0) return {};
        result.path[static_cast<size_t>(y - r.y0)] = best_x;
        bool row_has_ink = false;
        for (int x = left_at(r, y); x < right_at(r, y); ++x)
            row_has_ink = row_has_ink || mask[row + static_cast<size_t>(x)] != 0;
        if (!row_has_ink) continue;
        ++active_path_rows;
        int clear = 1;
        for (int x = best_x - 1; x >= left_at(r, y) && !mask[row + static_cast<size_t>(x)]; --x) ++clear;
        for (int x = best_x + 1; x < right_at(r, y) && !mask[row + static_cast<size_t>(x)]; ++x) ++clear;
        clear_widths.push_back(clear);
        if (clear >= std::max(12, rw / 16)) ++clear_rows;
    }
    const double clear_fraction = static_cast<double>(clear_rows) / std::max(1, active_path_rows);
    if (clear_fraction < 0.90) return {};

    // Both sides must contain substantial text support; reject paths that merely
    // isolate a margin or decorative stripe.
    uint64_t left_ink = 0, right_ink = 0;
    int left_active = 0, right_active = 0;
    int left_supported_rows = 0, right_supported_rows = 0;
    int left_runs = 0, right_runs = 0;
    bool in_left_run = false, in_right_run = false;
    for (int y = r.y0; y < r.y1; ++y) {
        const int seam = result.path[static_cast<size_t>(y - r.y0)];
        const size_t row = static_cast<size_t>(y) * width;
        bool la = false, ra = false;
        int row_total = 0, row_left = 0, row_right = 0;
        for (int x = left_at(r, y); x < right_at(r, y); ++x) {
            if (!mask[row + static_cast<size_t>(x)]) continue;
            ++row_total;
            if (x < seam) { ++left_ink; ++row_left; la = true; }
            else { ++right_ink; ++row_right; ra = true; }
        }
        if (row_total >= 4 && row_left >= std::max(2, row_total * 3 / 10)) ++left_supported_rows;
        if (row_total >= 4 && row_right >= std::max(2, row_total * 3 / 10)) ++right_supported_rows;
        if (la && !in_left_run) ++left_runs;
        if (ra && !in_right_run) ++right_runs;
        in_left_run = la;
        in_right_run = ra;
        left_active += la;
        right_active += ra;
    }
    const uint64_t side_min = std::max<uint64_t>(32, total_ink / 4);
    const int row_support_min = std::max(8, rh / 100);
    if (left_ink < side_min || right_ink < side_min || left_active < 3 || right_active < 3 ||
        left_supported_rows < row_support_min || right_supported_rows < row_support_min ||
        left_runs < 2 || right_runs < 2)
        return {};
    const int median_clear = [&]() {
        const auto m = clear_widths.begin() + static_cast<std::ptrdiff_t>(clear_widths.size() / 2);
        std::nth_element(clear_widths.begin(), m, clear_widths.end());
        return *m;
    }();
    if (median_clear < std::max(12, rw / 16)) return {};
    result.begin = result.end = median_clear / 2;
    result.width = median_clear;
    result.confidence = std::clamp(clear_fraction *
        (1.0 - static_cast<double>(best_cost) / std::max(1, ny)), 0.0, 1.0);
    return result;
}

inline Cut find_cut(const std::vector<uint8_t>& mask, int image_width,
                    const LayoutRegion& r, int page_height, bool curved = true) {
    const uint64_t ink = count_ink(mask, image_width, r);
    if (ink < 32) return {};
    std::vector<uint64_t> columns(static_cast<size_t>(r.x1 - r.x0), 0);
    std::vector<uint64_t> rows(static_cast<size_t>(r.y1 - r.y0), 0);
    for (int y = r.y0; y < r.y1; ++y) {
        const size_t row = static_cast<size_t>(y) * image_width;
        for (int x = left_at(r, y); x < right_at(r, y); ++x) {
            if (!mask[row + static_cast<size_t>(x)]) continue;
            ++columns[static_cast<size_t>(x - r.x0)];
            ++rows[static_cast<size_t>(y - r.y0)];
        }
    }
    const int region_width = r.x1 - r.x0;
    const int region_height = r.y1 - r.y0;
    const int horizontal_min = std::max(18, page_height / 70);
    Cut horizontal = widest_empty_cut(rows, 0, region_height, horizontal_min, false, ink);
    if (horizontal.found) { horizontal.begin += r.y0; horizontal.end += r.y0; }
    const int vertical_min = std::max(36, region_width / 20);
    Cut vertical = widest_empty_cut(columns, 0, region_width, vertical_min, true, ink);
    if (vertical.found) {
        vertical.begin += r.x0;
        vertical.end += r.x0;
        const int left_runs = active_row_runs(mask, image_width, r, r.x0, vertical.begin);
        const int right_runs = active_row_runs(mask, image_width, r, vertical.end, r.x1);
        const double center = (vertical.begin + vertical.end) / 2.0;
        const bool supported = left_runs >= 2 && right_runs >= 2;
        const bool center_gutter = vertical.width >= region_width * 0.04 &&
            std::abs(center - (r.x0 + r.x1) / 2.0) <= region_width * 0.08 &&
            left_runs >= 1 && right_runs >= 1;
        if (!(supported || center_gutter)) vertical = {};
    }
    if (!vertical.found && region_width >= image_width * 0.70 &&
        active_row_runs(mask, image_width, r, r.x0, r.x1) >= 6) {
        const int center_min = std::max(6, region_width / 160);
        const uint64_t tolerance = std::max<uint64_t>(1, static_cast<uint64_t>(region_height) / 100);
        Cut centered = widest_empty_cut(columns, 0, region_width, center_min, true, ink,
                                         tolerance, region_width * 47 / 100, region_width * 53 / 100);
        if (centered.found) {
            centered.begin += r.x0;
            centered.end += r.x0;
            if (active_row_runs(mask, image_width, r, r.x0, centered.begin) >= 2 &&
                active_row_runs(mask, image_width, r, centered.end, r.x1) >= 2) {
                const double vf = static_cast<double>(centered.width) / std::max(1, region_width);
                const double hf = horizontal.found ? static_cast<double>(horizontal.width) /
                    std::max(1, region_height) : 0.0;
                if (!(horizontal.found && hf > vf * 2.0)) vertical = centered;
            }
        }
    }
    if (vertical.found) return vertical;
    if (curved && !r.curved_left && !r.curved_right) {
        const Cut seam = curved_vertical_seam(mask, image_width, r, ink);
        if (seam.found) return seam;
    }
    if (horizontal.found) return horizontal;
    return {};
}

inline void update_bounds(LayoutRegion& r) {
    if (r.left_by_row.empty() || r.right_by_row.empty()) return;
    r.x0 = *std::min_element(r.left_by_row.begin(), r.left_by_row.end());
    r.x1 = *std::max_element(r.right_by_row.begin(), r.right_by_row.end());
}
inline void split_recursive(const std::vector<uint8_t>& mask, int image_width,
                            int page_height, const LayoutRegion& region,
                            bool right_to_left, int depth,
                            std::vector<LayoutRegion>& out, bool curved = true) {
    constexpr int max_depth = 18;
    constexpr size_t max_regions = 256;
    if (depth >= max_depth || out.size() >= max_regions) { out.push_back(region); return; }
    const Cut cut = find_cut(mask, image_width, region, page_height, curved);
    if (!cut.found) { out.push_back(region); return; }
    if (cut.vertical) {
        LayoutRegion left{region.x0, region.y0, region.x1, region.y1};
        LayoutRegion right{region.x0, region.y0, region.x1, region.y1};
        const int rh = region.y1 - region.y0;
        left.left_by_row.resize(static_cast<size_t>(rh));
        left.right_by_row.resize(static_cast<size_t>(rh));
        right.left_by_row.resize(static_cast<size_t>(rh));
        right.right_by_row.resize(static_cast<size_t>(rh));
        for (int y = region.y0; y < region.y1; ++y) {
            const size_t i = static_cast<size_t>(y - region.y0);
            const int lo = left_at(region, y), hi = right_at(region, y);
            int left_end = cut.begin, right_begin = cut.end;
            if (!cut.path.empty()) {
                left_end = cut.path[i];
                right_begin = left_end;
                left.curved_right = true;
                right.curved_left = true;
            }
            left.left_by_row[i] = lo;
            left.right_by_row[i] = std::clamp(left_end, lo + 1, hi);
            right.left_by_row[i] = std::clamp(right_begin, lo, hi - 1);
            right.right_by_row[i] = hi;
        }
        left.curved_left = region.curved_left;
        right.curved_right = region.curved_right;
        left.seam_confidence = right.seam_confidence = cut.path.empty()
            ? region.seam_confidence : cut.confidence;
        update_bounds(left);
        update_bounds(right);
        if (right_to_left) {
            split_recursive(mask, image_width, page_height, right, right_to_left, depth + 1, out, curved);
            split_recursive(mask, image_width, page_height, left, right_to_left, depth + 1, out, curved);
        } else {
            split_recursive(mask, image_width, page_height, left, right_to_left, depth + 1, out, curved);
            split_recursive(mask, image_width, page_height, right, right_to_left, depth + 1, out, curved);
        }
    } else {
        const int top_h = cut.begin - region.y0;
        const int bottom_start = cut.end;
        if (top_h <= 0 || bottom_start >= region.y1) { out.push_back(region); return; }
        LayoutRegion top{region.x0, region.y0, region.x1, cut.begin};
        LayoutRegion bottom{region.x0, cut.end, region.x1, region.y1};
        if (!region.left_by_row.empty()) {
            top.left_by_row.assign(region.left_by_row.begin(), region.left_by_row.begin() + top_h);
            top.right_by_row.assign(region.right_by_row.begin(), region.right_by_row.begin() + top_h);
            bottom.left_by_row.assign(region.left_by_row.begin() + (bottom_start - region.y0), region.left_by_row.end());
            bottom.right_by_row.assign(region.right_by_row.begin() + (bottom_start - region.y0), region.right_by_row.end());
            top.curved_left = bottom.curved_left = region.curved_left;
            top.curved_right = bottom.curved_right = region.curved_right;
            top.seam_confidence = bottom.seam_confidence = region.seam_confidence;
            update_bounds(top);
            update_bounds(bottom);
        }
        split_recursive(mask, image_width, page_height, top, right_to_left, depth + 1, out, curved);
        split_recursive(mask, image_width, page_height, bottom, right_to_left, depth + 1, out, curved);
    }
}
inline std::vector<LayoutRegion> detect_regions(const std::vector<uint8_t>& mask,
                                                int width, int height,
                                                bool right_to_left,
                                                bool curved = true) {
    std::vector<LayoutRegion> regions;
    split_recursive(mask, width, height, LayoutRegion{0, 0, width, height},
                    right_to_left, 0, regions, curved);
    return regions;
}
} // namespace lineforge_layout
