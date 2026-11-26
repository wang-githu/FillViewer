REM ============================================================

REM  说明：本文件为示例，链数据/变量名请按你自己的项目重新生成。

REM  用法见 docs/WinCC_管道流水动画_方案与教程.md

REM ============================================================

Option Explicit

' 安装目录：改成你实际解压/部署的位置（末尾不要带反斜杠）
' 改这一行即可，下面的 tags/data.json/调试日志路径都由它拼出来
Const ROOT = "<安装目录>"

'==============================================================
'  WinCC 全局脚本动作：把实时变量写进 FillViewer 的 data.json
'  变量清单不用再手工维护 —— 直接读 <安装目录>\data\tags.txt
'  （网页上每给一块看板填了“变量”，就会自动把清单写进 tags.txt）
'  触发器：周期触发，500 毫秒
'--------------------------------------------------------------
'  部署：把这个文件【整段】粘贴替换掉动作里的全部内容，
'        再给动作加触发器 周期 500 毫秒 → 编译 → 保存 → 激活
'==============================================================
Function action
    Dim TAGS_PATH, OUT_PATH, ERR_PATH
    TAGS_PATH = ROOT & "\data\tags.txt"
    OUT_PATH  = ROOT & "\data\data.json"
    ERR_PATH  = ROOT & "\data\_wincc_debug.txt"

    Dim fso, tf, f, s, line, tags, nTag, i, j
    Dim v, item, tagName, ch, codeN, esc, dbg, bad, errN, errD
    Dim FALLBACK
    FALLBACK = Array("Demo_YL1YW1_PJ1")

    On Error Resume Next

    Set fso = CreateObject("Scripting.FileSystemObject")
    If Err.Number <> 0 Then Exit Function

    ' ---------- 1. 读变量清单（网页写出的 UTF-16） ----------
    nTag = 0
    ReDim tags(0)
    Set tf = fso.OpenTextFile(TAGS_PATH, 1, False, -1)   ' 1=读 -1=Unicode
    If Err.Number <> 0 Then
        Err.Clear
        ReDim tags(UBound(FALLBACK))
        For i = 0 To UBound(FALLBACK)
            tags(i) = FALLBACK(i)
        Next
        nTag = UBound(FALLBACK) + 1
    Else
        Do While Not tf.AtEndOfStream
            line = tf.ReadLine
            line = Replace(line, vbCr, "")
            line = Replace(line, vbLf, "")
            line = Trim(line)
            If Len(line) > 0 Then
                If Left(line, 1) <> "#" Then
                    ReDim Preserve tags(nTag)
                    tags(nTag) = line
                    nTag = nTag + 1
                End If
            End If
        Loop
        tf.Close
        If nTag = 0 Then
            ReDim tags(UBound(FALLBACK))
            For i = 0 To UBound(FALLBACK)
                tags(i) = FALLBACK(i)
            Next
            nTag = UBound(FALLBACK) + 1
        End If
    End If

    ' ---------- 2. 逐个读值，拼 JSON ----------
    s = "{""ts"": " & CLng(Timer) & ", ""values"": {"
    dbg = "变量清单 (" & nTag & " 个)  时间 " & Time & vbCrLf & "----------------" & vbCrLf
    bad = 0

    For i = 0 To nTag - 1
        tagName = tags(i)

        ' 变量名 -> \uXXXX（纯 ASCII，防乱码）
        esc = ""
        For j = 1 To Len(tagName)
            ch = Mid(tagName, j, 1)
            codeN = AscW(ch)
            If codeN < 0 Then codeN = codeN + 65536
            If codeN > 127 Then
                esc = esc & "\u" & Right("000" & Hex(codeN), 4)
            ElseIf ch = """" Then
                esc = esc & "\"""
            ElseIf ch = "\" Then
                esc = esc & "\\"
            Else
                esc = esc & ch
            End If
        Next

        ' 读值
        errN = 0 : errD = ""
        Err.Clear
        Set item = Nothing
        Set item = HMIRuntime.Tags(tagName)
        If Err.Number <> 0 Then
            errN = Err.Number : errD = Err.Description
            Err.Clear
        End If
        v = 0
        If IsObject(item) Then
            v = item.Read
            If Err.Number <> 0 Then
                errN = Err.Number : errD = Err.Description
                Err.Clear
            End If
        ElseIf errN = 0 Then
            errN = -1 : errD = "HMIRuntime.Tags 没有返回对象"
        End If
        If IsNull(v) Or IsEmpty(v) Then v = 0
        If Not IsNumeric(v) Then v = 0
        v = Replace(CStr(Round(CDbl(v), 2)), ",", ".")

        If i > 0 Then s = s & ", "
        s = s & """" & esc & """: " & v

        dbg = dbg & "  " & tagName & "  =  " & v
        If errN <> 0 Then
            bad = bad + 1
            dbg = dbg & "    << 读取失败 " & errN & " " & errD
        End If
        dbg = dbg & vbCrLf
    Next
    s = s & "}}"

    ' ---------- 3. 写 data.json ----------
    Err.Clear
    Set f = fso.CreateTextFile(OUT_PATH, True, False)   ' ASCII
    If Err.Number = 0 Then
        f.Write s
        f.Close
    End If
    Set f = Nothing

    ' ---------- 4. 写调试文件（读不出值时看这个） ----------
    Err.Clear
    Set f = fso.CreateTextFile(ERR_PATH, True, True)    ' Unicode，记事本能直接看中文
    If Err.Number = 0 Then
        f.Write dbg & vbCrLf & "失败个数: " & bad & vbCrLf
        f.Close
    End If
    Set f = Nothing
    Set fso = Nothing
    On Error Goto 0
End Function
