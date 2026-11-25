REM ============================================================
REM  说明：本文件为示例，链数据/变量名请按你自己的项目重新生成。
REM  用法见 docs/WinCC_管道流水动画_方案与教程.md
REM ============================================================
' ==============================================================
'  WinCC 管道流水动画  ——  方案 A：逐段高亮推进（原生 3D 控件）
'  自动生成：基于本项目 Demo_Pipe_seg01~seg36 的实测坐标
'                                                               
'  部署（4 步）：                                               
'   1. 图形编辑器 → 全局脚本 → VBS 编辑器                      
'   2. 项目模块下「新建动作」，全选删掉默认内容                
'   3. 粘贴本文件全部内容                                       
'   4. 给动作加触发器：周期触发 250 毫秒 → 编译 → 激活         
'                                                               
'  想改流速 / 颜色 / 光带长度，只动下面「现场可调参数」5 行     
' ==============================================================
Option Explicit

Const CTRL_NAME  = "cwc1"                 ' 3D 控件对象名
Const SPEED_MS   = 8                       ' 流速（米/秒）
Const TAIL_M     = 1.2                     ' 光带长度（米）
Const TICK_MS    = 250                     ' 触发周期（毫秒），必须与触发器一致
Const COLOR_RUN  = "rgb(0,255,220)"        ' 流动颜色
Const RUNNING    = True                    ' False = 全部停流（接 PLC 时可替换）

' ---------- 管道链数据（自动生成，勿手改） ----------
' 每条链是一串 "对象名|起点弧长|终点弧长"（单位：米）
Dim RT(3)
RT(0) = Array("Demo_Pipe_seg14|0.000|4.903", "Demo_Pipe_seg13|4.903|5.868", "Demo_Pipe_seg20|5.868|12.922", "Demo_Pipe_seg07|12.922|19.177", "Demo_Pipe_seg08|19.177|21.733", "Demo_Pipe_seg06|21.733|27.346", "Demo_Pipe_seg12|27.346|27.446", "Demo_Pipe_seg30|27.446|28.694", "Demo_Pipe_seg11|28.694|33.069", "Demo_Pipe_seg10|33.069|39.529", "Demo_Pipe_seg09|39.529|45.993", "Demo_Pipe_seg19|45.993|49.369", "Demo_Pipe_seg21|49.369|58.793", "Demo_Pipe_seg01|58.793|63.998", "Demo_Pipe_seg02|63.998|68.160", "Demo_Pipe_seg18|68.160|72.536", "Demo_Pipe_seg17|72.536|75.912", "Demo_Pipe_seg22|75.912|81.596", "Demo_Pipe_seg16|81.596|95.867", "Demo_Pipe_seg15|95.867|99.523", "Demo_Pipe_seg04|99.523|102.659", "Demo_Pipe_seg05|102.659|103.139", "Demo_Pipe_seg28|103.139|107.236", "Demo_Pipe_seg31|107.236|108.688", "Demo_Pipe_seg29|108.688|112.671", "Demo_Pipe_seg25|112.671|113.151", "Demo_Pipe_seg24|113.151|116.287", "Demo_Pipe_seg23|116.287|127.137", "Demo_Pipe_seg04.001|127.137|129.007", "Demo_Pipe_seg03|129.007|133.180")
RT(1) = Array("Demo_Pipe_seg26|0.000|1.871", "Demo_Pipe_seg27|1.871|4.996")
RT(2) = Array("Demo_Pipe_seg33|0.000|1.091", "Demo_Pipe_seg32|1.091|1.825")
RT(3) = Array("Demo_Pipe_seg35|0.000|1.091", "Demo_Pipe_seg34|1.091|1.825")

' ---------- 主程序 ----------
Dim ctl, tauA, tNow, stepM, i, j, p, nm, s1, rTotal, head
Dim xx, aa, bb, hit, onW, offW

On Error Resume Next
Set ctl = Nothing
Set ctl = ScreenItems(CTRL_NAME)
If ctl Is Nothing Then Set ctl = Item.Parent.ScreenItems(CTRL_NAME)
If ctl Is Nothing Then Exit Function
If Not RUNNING Then Exit Function

tNow  = Timer
stepM = SPEED_MS * TICK_MS / 1000
If stepM <= 0 Then stepM = 0.01

For i = 0 To UBound(RT)
    rTotal = 0
    For j = 0 To UBound(RT(i))
        p = Split(RT(i)(j), "|")
        If UBound(p) >= 2 Then
            If CDbl(p(2)) > rTotal Then rTotal = CDbl(p(2))
        End If
    Next
    If rTotal > 0 Then
        head = tNow * SPEED_MS
        head = head - Int(head / rTotal) * rTotal        ' 绕圈取余

        For j = 0 To UBound(RT(i))
            p  = Split(RT(i)(j), "|")
            nm = p(0)
            s1 = CDbl(p(2))
            xx = s1 - Int(s1 / rTotal) * rTotal

            ' ① 新点亮：段末端落在 [head-step, head) 区间
            aa = head - stepM
            aa = aa - Int(aa / rTotal) * rTotal
            bb = head
            hit = False
            If aa <= bb Then
                If xx >= aa And xx < bb Then hit = True
            Else
                If xx >= aa Or xx < bb Then hit = True
            End If
            If hit Then ctl.SetObjectMaterialColorExt nm, True, COLOR_RUN, 1, 1.0, 0

            ' ② 新熄灭：段末端落在 [head-step-TAIL, head-TAIL) 区间
            aa = head - stepM - TAIL_M
            aa = aa - Int(aa / rTotal) * rTotal
            bb = head - TAIL_M
            bb = bb - Int(bb / rTotal) * rTotal
            hit = False
            If aa <= bb Then
                If xx >= aa And xx < bb Then hit = True
            Else
                If xx >= aa Or xx < bb Then hit = True
            End If
            If hit Then ctl.SetObjectMaterialColorExt nm, False, "", 1, 1.0, 0
        Next
    End If
Next

Err.Clear
On Error Goto 0

' ---------- 备注 ----------
' 1) 每帧只对「状态发生变化的段」发一次调用（约 2~6 次/帧），
'    不会因为 36 段全刷而拖慢运行系统。
' 2) 停流：把 RUNNING 改成 False；或整段注释掉，用下面的辅助代码一次性恢复原色：
'      For i = 0 To UBound(RT)
'          For j = 0 To UBound(RT(i))
'              p = Split(RT(i)(j), "|")
'              ctl.SetObjectMaterialColorExt p(0), False, "", 1, 1.0, 0
'          Next
'      Next
' 3) 按流量调速：把 SPEED_MS 换成变量，例如
'      SPEED_MS = HMIRuntime.Tags("Demo_XS1LL1_PJ1").Read * 0.5
'    （需要把 Const 改成 Dim）