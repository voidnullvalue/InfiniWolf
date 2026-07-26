"""Build-time values stamped into packaged executables by CI."""

# Source checkouts resolve their current Git commit dynamically. Release
# builds replace this empty value before PyInstaller freezes the package.
COMMIT = ""
