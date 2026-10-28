// Behavioral probe for the pinned upstream Costmap2DPublisher.
// This harness is supplied by Robot Debug Lab; it is not an upstream test.
#include <chrono>
#include <iostream>
#include <string>
#include <thread>
#include "nav2_costmap_2d/costmap_2d_publisher.hpp"
#include "nav2_costmap_2d/costmap_layer.hpp"

using namespace std::chrono_literals;

class ProbeLayer : public nav2_costmap_2d::CostmapLayer
{
public:
  void enable(bool value) {enabled_ = value;}
  void reset() override {}
  bool isClearable() override {return true;}
  void updateBounds(double, double, double, double *, double *, double *, double *) override {}
  void updateCosts(nav2_costmap_2d::Costmap2D &, int, int, int, int) override {}
};

int main(int argc, char ** argv)
{
  if (argc != 2) {return 2;}
  const std::string scenario = argv[1];
  rclcpp::init(argc, argv);
  auto parent = std::make_shared<nav2_util::LifecycleNode>("publisher_probe", "");
  auto reader = std::make_shared<rclcpp::Node>("evidence_reader");
  ProbeLayer layer;
  layer.resizeMap(4, 4, 0.05, 0.0, 0.0);
  layer.enable(true);
  // Distinct, known costs let us reject patches that publish empty/wrong data.
  for (unsigned int y = 0; y < 4; ++y) {
    for (unsigned int x = 0; x < 4; ++x) {layer.setCost(x, y, 10 + 4 * y + x);}
  }
  nav2_costmap_2d::Costmap2DPublisher publisher(parent, &layer, "map", "probe", false);
  publisher.on_activate();
  unsigned int received = 0;
  bool correct = false;
  auto qos = rclcpp::QoS(1).transient_local().reliable();
  auto raw = reader->create_subscription<nav2_msgs::msg::CostmapUpdate>(
    "probe_raw_updates", qos, [&](nav2_msgs::msg::CostmapUpdate::ConstSharedPtr msg) {
      if (scenario == "valid_grid") {return;}
      ++received;
      correct = msg->x == 1 && msg->y == 1 && msg->size_x == 2 && msg->size_y == 2 &&
        msg->data == std::vector<unsigned char>({15, 16, 19, 20});
    });
  auto grid = reader->create_subscription<map_msgs::msg::OccupancyGridUpdate>(
    "probe_updates", qos, [&](map_msgs::msg::OccupancyGridUpdate::ConstSharedPtr msg) {
      if (scenario != "valid_grid") {return;}
      ++received;
      correct = msg->x == 1 && msg->y == 1 && msg->width == 2 && msg->height == 2 &&
        msg->data == std::vector<int8_t>({6, 6, 7, 8});
    });
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(parent->get_node_base_interface());
  executor.add_node(reader);
  const auto until = std::chrono::steady_clock::now() + 8s;
  while ((reader->count_publishers("probe_raw_updates") == 0 ||
    parent->count_subscribers("probe_raw_updates") == 0 ||
    parent->count_subscribers("probe_updates") == 0) && std::chrono::steady_clock::now() < until)
  {
    executor.spin_some();
    std::this_thread::sleep_for(20ms);
  }
  if (parent->count_subscribers("probe_raw_updates") == 0 ||
    parent->count_subscribers("probe_updates") == 0)
  {
    std::cerr << "INFRASTRUCTURE_ERROR: subscriber discovery failed\n";
    return 3;
  }
  // First publication initializes cached geometry before the incremental path.
  publisher.publishCostmap();
  if (scenario == "disabled") {layer.enable(false);}
  if (scenario == "empty") {
    layer.resizeMap(0, 0, 0.05, 0.0, 0.0);
    publisher.publishCostmap();
  }
  const bool invalid = scenario == "overflow" || scenario == "empty" || scenario == "recovery";
  publisher.updateBounds(invalid ? 0 : 1, invalid ? 8 : 3, invalid ? 0 : 1, invalid ? 8 : 3);
  std::cout << "PROBE scenario=" << scenario << " layer=" << layer.getSizeInCellsX() <<
    "x" << layer.getSizeInCellsY() << " bounds=" << (invalid ? "0,8,0,8" : "1,3,1,3") <<
    " enabled=" << layer.isEnabled() << std::endl;
  publisher.publishCostmap();
  auto drain = [&]() {
      const auto deadline = std::chrono::steady_clock::now() + 350ms;
      while (std::chrono::steady_clock::now() < deadline) {
        executor.spin_some();
        std::this_thread::sleep_for(10ms);
      }
    };
  drain();
  if (scenario == "recovery") {
    if (received != 0) {std::cerr << "FAIL: invalid update was published\n"; return 1;}
    publisher.updateBounds(1, 3, 1, 3);
    publisher.publishCostmap();
    drain();
  }
  const bool expect_update = scenario == "valid_raw" || scenario == "valid_grid" || scenario == "recovery";
  const bool passed = expect_update ? received == 1 && correct : received == 0;
  std::cout << "RESULT " << (passed ? "PASS" : "FAIL") << " received=" << received <<
    " correct_payload=" << correct << std::endl;
  publisher.on_deactivate();
  rclcpp::shutdown();
  return passed ? 0 : 1;
}
