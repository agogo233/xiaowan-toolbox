"""小丸工具箱应用包。

版本号是发布与打包的单一来源：
CI 会校验 git tag（去掉 v 前缀）必须与本文件一致；
Inno Setup 安装包可通过 ISCC /DMYAPP_VERSION 覆盖，默认同步这里的值。
"""

__version__ = "2.2.0"
