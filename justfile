default:
    echo "Available recipes: build, test, clean"

build:
    sudo rm -rf node_modules && .vscode/build.sh

test:
    scp "out/Overlay Launcher.zip" deck@192.168.0.241:~/Desktop

clean:
    sudo rm -rf node_modules dist
    sudo rm -rf /tmp/decky