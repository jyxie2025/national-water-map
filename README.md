# 全国河道站实时水位 Leaflet 地图

这个项目从全国水雨情信息网站抓取全国河道站实时水位，长期累积站点水位历史，并生成可部署到 GitHub Pages 的 Leaflet + Plotly 静态网页。

数据来源：http://xxfb.mwr.cn/sq_djdh.html

## 功能

- Leaflet 地图展示全国河道站实时水位，站点不再按河流或流域连接成线。
- 手机端优先展示地图、定位和站点详情，筛选面板默认收起。
- 支持地图、卫星遥感、浅色地图和地形图图层切换。
- 站点颜色按最近一次水位相较前一次记录的趋势显示：上涨、下降、稳定或暂无实时数据。
- 点击站点查看近期和长期水位变化，Plotly 图表支持缩放、拖拽和范围滑块。
- 趋势图支持 1周、1月、3个月、6个月、1年、全部和自定义年数查看。
- 桌面端和手机端都通过“筛选”按钮展开流域、行政区划、河流和站点筛选。
- GitHub Actions 每半小时自动运行一次，发现新时间点数据后写入历史记录并更新 Pages。

## 数据文件

- `data/stations.csv`：站点坐标缓存。
- `data/river_water_levels.csv`：长期累积的历史水位。
- `data/latest_river_water_levels.json`：最新抓取快照。
- `docs/data/stations.json`：网页使用的站点数据。
- `docs/data/latest.json`：网页使用的最新水位。
- `docs/data/history.csv`：网页使用的历史水位。
- `docs/index.html`：GitHub Pages 页面。

## 说明

官网接口已经返回站点经纬度，因此当前版本直接使用水利部全国水雨情信息站点坐标，不额外调用第三方地理编码。页面只展示站点，不绘制同一河流或同一流域的监测点连线。

## 部署

仓库推送到 `jyxie2025/national-water-map` 后，GitHub Actions 会自动抓取数据、提交更新，并启用 GitHub Pages。

Pages 地址：

```text
https://jyxie2025.github.io/national-water-map/
```
