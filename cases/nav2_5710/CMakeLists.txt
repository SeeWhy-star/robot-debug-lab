cmake_minimum_required(VERSION 3.16)
project(rdl_nav2_5710 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
find_package(ament_cmake REQUIRED)
find_package(nav2_util REQUIRED)
find_package(nav2_msgs REQUIRED)
find_package(map_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(tf2_geometry_msgs REQUIRED)
set(SOURCE_ROOT /work/source/nav2_costmap_2d)
# Compile the actual pinned upstream implementation, not a rewritten algorithm.
add_executable(publisher_probe
  probe.cpp
  ${SOURCE_ROOT}/src/costmap_2d_publisher.cpp
  ${SOURCE_ROOT}/src/costmap_2d.cpp
  ${SOURCE_ROOT}/src/costmap_layer.cpp
  ${SOURCE_ROOT}/src/layer.cpp)
target_include_directories(publisher_probe PRIVATE ${SOURCE_ROOT}/include)
ament_target_dependencies(publisher_probe nav2_util nav2_msgs map_msgs nav_msgs tf2_geometry_msgs)
target_compile_options(publisher_probe PRIVATE -g -O1 -fno-omit-frame-pointer -fsanitize=address,undefined)
target_link_options(publisher_probe PRIVATE -fsanitize=address,undefined)
