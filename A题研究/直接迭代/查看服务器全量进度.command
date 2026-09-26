#!/bin/zsh
ssh -o BatchMode=yes -o ConnectTimeout=8 devenvc_8ifww.b8d4401cf00144eabfaf36b41bbbb14b.atomgit.0 '/mnt/workspace/mathmodel_a_full_20260926/venv/bin/python /mnt/workspace/mathmodel_a_full_20260926/project/A题研究/直接迭代/show_server_full.py --root /mnt/workspace/mathmodel_a_full_20260926/full_run --pid-file /mnt/workspace/mathmodel_a_full_20260926/full_run.pid'
read '?按回车关闭；再次打开会读取服务器最新状态。'
