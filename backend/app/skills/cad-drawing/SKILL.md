---
name: cad-drawing
description: CAD 机械制图：用 ezdxf 画机械零件图（矩形/圆/孔/标注/图层），导出 DXF，可用 AutoCAD/FreeCAD 打开。
keywords: cad,cad图,机械图,机械制图,机械,底板,法兰,零件,制图,画图,图纸,零件图,dxf,机械设计,工程图,autocad,freecad,机械零件,加工图,绘图
---

# CAD 机械制图技能指南（ezdxf）

本任务属于「CAD 制图」技能，请用 Python 的 ezdxf 库绘制并导出 DXF 文件。

## 工具
- `import ezdxf`（已安装），无需外部软件。
- 创建文档：`doc = ezdxf.new("R2010")`；画图空间：`msp = doc.modelspace()`

## 常用绘图 API
- 直线：`msp.add_line((x1, y1), (x2, y2))`
- 圆：`msp.add_circle((cx, cy), radius)`
- 矩形/多边形：`msp.add_lwpolyline([(x1,y1), (x2,y2), ...], close=True)`
- 圆弧：`msp.add_arc((cx, cy), radius, start_angle, end_angle)`
- 文字：`msp.add_text("内容", height=2.5).set_placement((x, y))`
- 尺寸标注：`dim = msp.add_linear_dim(base=(x0, y0), p1=(x1, y1), p2=(x2, y2), dimstyle="EZDXF"); dim.render()`
- 图层（中心线/虚线）：`doc.layers.add("CENTER", color=1, linetype="CENTER")`；画线时加 `dxfattribs={"layer": "CENTER"}`
- 中心线/虚线用内置线型：`CENTER`、`DASHED`（ezdxf 自带）

## 制图规范
- 严格按需求尺寸绘图（单位 mm），关键尺寸必须标注（长/宽/直径/圆角/孔距）
- 常见机械件画法：
  - 底板：矩形 + 四角圆角（可用 `msp.add_lwpolyline` 手算圆角或简化直角）+ 四个通孔（`add_circle`）
  - 法兰：外圆 + 中心圆 + 圆周均布孔（用三角函数算孔位）
  - 轴/圆盘：同心圆 + 中心线
- 中心线（CENTER 线型）画在圆/孔的中心位置

## 输出要求
- 导出：`doc.saveas("dxf_xxx.dxf")`（**dxf_ 前缀**，写到当前工作目录）
- 成功后打印 `[OUTPUT_FILE] <完整路径>` 或 `[OUTPUT_URL] /dxf_xxx.dxf`
- 失败打印 `[ERROR] <原因>`
