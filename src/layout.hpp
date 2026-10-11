#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <numeric>
#include <vector>

struct LayoutRegion {
    int x0 = 0;
    int y0 = 0;
    int x1 = 0; // exclusive
    int y1 = 0; // exclusive
};

namespace lineforge_layout {

struct Cut {
    bool found = false;
    bool vertical = false;
    int begin = 0;
    int end = 0;
    int width = 0;
};

inline uint64_t count_ink(const std::vector<uint8_t>& mask, int image_width,
                          const LayoutRegion& r) {
    uint64_t total = 0;
    for (int y = r.y0; y < r.y1; ++y) {
        const size_t row = static_cast<size_t>(y) * image_width;
        for (int x = r.x0; x < r.x1; ++x)
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
        for (int x = x0; x < x1; ++x) {
            if (mask[row + static_cast<size_t>(x)]) { row_active = true; break; }
        }
        if (row_active && !active) ++runs;
        active = row_active;
    }
    return runs;
}

inline Cut widest_empty_cut(const std::vector<uint64_t>& projection,
                            int start, int end, int min_width,
                            bool vertical, uint64_t total_ink) {
    Cut best;
    const int side_guard = std::max(2, (end - start) / 100);
    std::vector<uint64_t> prefix(projection.size() + 1, 0);
    for (size_t i = 0; i < projection.size(); ++i)
        prefix[i + 1] = prefix[i] + projection[i];
    for (int i = start + side_guard; i < end - side_guard;) {
        if (projection[static_cast<size_t>(i)] != 0) {
            ++i;
            continue;
        }
        const int gap_begin = i;
        while (i < end - side_guard && projection[static_cast<size_t>(i)] == 0) ++i;
        const int gap_end = i;
        const int gap_width = gap_end - gap_begin;
        if (gap_width < min_width || gap_begin <= start || gap_end >= end) continue;

        const uint64_t before = prefix[static_cast<size_t>(gap_begin)] - prefix[static_cast<size_t>(start)];
        const uint64_t after = prefix[static_cast<size_t>(end)] - prefix[static_cast<size_t>(gap_end)];
        const uint64_t min_side = std::max<uint64_t>(8, total_ink / 100);
        if (before < min_side || after < min_side) continue;

        // The widest gutter is the most stable structural cue in noisy scans.
        if (!best.found || gap_width > best.width) {
            best = Cut{true, vertical, gap_begin, gap_end, gap_width};
        }
    }
    return best;
}

inline Cut find_cut(const std::vector<uint8_t>& mask, int image_width,
                    const LayoutRegion& r, int page_height) {
    const uint64_t ink = count_ink(mask, image_width, r);
    if (ink < 32) return {};

    std::vector<uint64_t> columns(static_cast<size_t>(r.x1 - r.x0), 0);
    std::vector<uint64_t> rows(static_cast<size_t>(r.y1 - r.y0), 0);
    for (int y = r.y0; y < r.y1; ++y) {
        const size_t row = static_cast<size_t>(y) * image_width;
        for (int x = r.x0; x < r.x1; ++x) {
            if (!mask[row + static_cast<size_t>(x)]) continue;
            ++columns[static_cast<size_t>(x - r.x0)];
            ++rows[static_cast<size_t>(y - r.y0)];
        }
    }

    // Detect a column gutter before paragraph/line spacing. This makes a book or
    // newspaper page column-major in reading order even when lines align by row.
    const int region_width = r.x1 - r.x0;
    const int vertical_min = std::max(20, region_width / 40);
    Cut vertical = widest_empty_cut(columns, 0, region_width, vertical_min, true, ink);
    if (vertical.found) {
        vertical.begin += r.x0;
        vertical.end += r.x0;
        // Word spaces can be wider than a font-specific gutter on a single
        // baseline. A real text-column boundary normally has text on both
        // sides across multiple line rows.
        if (active_row_runs(mask, image_width, r, r.x0, vertical.begin) >= 2 &&
            active_row_runs(mask, image_width, r, vertical.end, r.x1) >= 2)
            return vertical;
    }

    // Horizontal cuts are intentionally more conservative than line finding.
    // They split wide blank gutters between panels/articles, not ordinary line gaps.
    const int region_height = r.y1 - r.y0;
    const int horizontal_min = std::max(18, page_height / 70);
    Cut horizontal = widest_empty_cut(rows, 0, region_height, horizontal_min, false, ink);
    if (horizontal.found) {
        horizontal.begin += r.y0;
        horizontal.end += r.y0;
    }
    return horizontal;
}

inline void split_recursive(const std::vector<uint8_t>& mask, int image_width,
                            int page_height, const LayoutRegion& region,
                            bool right_to_left, int depth,
                            std::vector<LayoutRegion>& out) {
    constexpr int max_depth = 18;
    constexpr size_t max_regions = 256;
    if (depth >= max_depth || out.size() >= max_regions) {
        out.push_back(region);
        return;
    }

    const Cut cut = find_cut(mask, image_width, region, page_height);
    if (!cut.found) {
        out.push_back(region);
        return;
    }

    if (cut.vertical) {
        LayoutRegion left{region.x0, region.y0, cut.begin, region.y1};
        LayoutRegion right{cut.end, region.y0, region.x1, region.y1};
        if (right_to_left) {
            split_recursive(mask, image_width, page_height, right, right_to_left, depth + 1, out);
            split_recursive(mask, image_width, page_height, left, right_to_left, depth + 1, out);
        } else {
            split_recursive(mask, image_width, page_height, left, right_to_left, depth + 1, out);
            split_recursive(mask, image_width, page_height, right, right_to_left, depth + 1, out);
        }
    } else {
        LayoutRegion top{region.x0, region.y0, region.x1, cut.begin};
        LayoutRegion bottom{region.x0, cut.end, region.x1, region.y1};
        split_recursive(mask, image_width, page_height, top, right_to_left, depth + 1, out);
        split_recursive(mask, image_width, page_height, bottom, right_to_left, depth + 1, out);
    }
}

inline std::vector<LayoutRegion> detect_regions(const std::vector<uint8_t>& mask,
                                                int width, int height,
                                                bool right_to_left) {
    std::vector<LayoutRegion> regions;
    split_recursive(mask, width, height, LayoutRegion{0, 0, width, height},
                    right_to_left, 0, regions);
    return regions;
}

} // namespace lineforge_layout
