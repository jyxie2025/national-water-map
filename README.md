# 全国河道站实时水位 Leaflet 地图

这个项目每天从全国水雨情信息网站抓取河道站实时水位，保存站点坐标缓存和长期历史记录，并生成一个可部署到 GitHub Pages 的 Leaflet + Plotly 静态站点。

数据来源：<http://xxfb.mwr.cn/sq_djdh.html>

## 功能

- Leaflet 动态地图显示全国河道站实时水位。
- 支持按流域、行政区划、河流、站点关键字筛选。
- 点击站点显示近期/长期水位变化图。
- 趋势图支持 1周、1月、3个月、6个月、1年、全部和自定义 N 年。
- 地图区分近三日有效、历史读数、暂无有效水位三类状态。
- 每天北京时间 09:30 和 17:30 通过 GitHub Actions 自动更新。

## 数据文件

- `data/stations.csv`：站点坐标缓存。
- `data/river_water_levels.csv`：长期累积历史水位。
- `data/latest_river_water_levels.json`：最新抓取快照。
- `docs/data/*.json|csv|geojson`：页面使用的数据。
- `docs/index.html`：GitHub Pages 页面。

## 说明

官网接口返回站点经纬度，因此本项目不使用第三方地理编码。地图中的河线是按同名河流站点坐标生成的站点序列线，用于让站点在可视化中与对应河流线重叠；如果后续加入权威河网 GeoJSON，可以在脚本中把站点进一步 snap 到真实河网。

## 部署

1. 在 GitHub 新建一个空仓库，例如 `jyxie2025/national-water-map`。
2. 在本目录执行：

```powershell
git init
git branch -M main
git add .
git commit -m "Add national river water map"
git remote add origin https://github.com/jyxie2025/national-water-map.git
git push -u origin main
```

3. 仓库推送后，GitHub Actions 会自动抓取数据、提交更新，并启用 GitHub Pages。
4. Pages 链接通常是 `https://jyxie2025.github.io/national-water-map/`。
