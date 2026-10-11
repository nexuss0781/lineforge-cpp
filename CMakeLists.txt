cmake_minimum_required(VERSION 3.16)
project(lineforge VERSION 1.0.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(JPEG REQUIRED)
find_package(PNG REQUIRED)

add_executable(linecrop src/main.cpp)
target_link_libraries(linecrop PRIVATE JPEG::JPEG PNG::PNG)
target_compile_definitions(linecrop PRIVATE LINEFORGE_VERSION=\"${PROJECT_VERSION}\")
target_compile_options(linecrop PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)

include(CTest)
if(BUILD_TESTING)
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  add_test(NAME synthetic_benchmark
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/benchmark.py
            --binary $<TARGET_FILE:linecrop>
            --work-dir ${CMAKE_CURRENT_BINARY_DIR}/benchmark)
  set_tests_properties(synthetic_benchmark PROPERTIES TIMEOUT 180)
  add_test(NAME real_page_eval_unit
    COMMAND ${Python3_EXECUTABLE} -m unittest discover
            -s ${CMAKE_CURRENT_SOURCE_DIR}/tests -p test_real_page_eval.py)
  add_test(NAME batch_output_regression
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/batch_output.py
            --binary $<TARGET_FILE:linecrop>
            --work-dir ${CMAKE_CURRENT_BINARY_DIR}/batch-output)
  set_tests_properties(batch_output_regression PROPERTIES TIMEOUT 180)
endif()
