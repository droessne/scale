#!/usr/bin/env bash

cd "${0%/*}"

COMPARE="find app test *.js* -type f -exec md5sum {} \; | sort -k 2"

if [ -z "$TRAVIS_TAG" ]
then
    echo "No tag set."
else
    echo "Tag of $TRAVIS_TAG set."
    TAG_SUFFIX = "_$TRAVIS_TAG"
fi
HASH_NAME="ui-code-hash$TAG_SUFFIX.md5"
EXISTING="https://s3.amazonaws.com/ais-public-artifacts/scale-ui/$HASH_NAME"

echo "Comparing $EXISTING and local files for changes..."
PREVIOUS=`curl -L -s $EXISTING`
CURRENT=`eval $COMPARE`

if [ "$PREVIOUS" == "$CURRENT" ]
then
    echo "No change in UI code detected. Skipping build."
else
    echo "UI changes detected. Executing build..."
    npm install -g gulp
    npm install
    if [ -z "$TRAVIS_TAG" ]
    then
        echo "Running regular deploy off master."
        gulp deploy
    else
        echo "Tag detected. Building a tagged deploy."
        gulp deploy --tag=$TRAVIS_TAG
    fi
    eval $COMPARE > deploy/$HASH_NAME
fi

