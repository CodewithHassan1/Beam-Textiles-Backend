from whitenoise.storage import CompressedManifestStaticFilesStorage

class SafeCompressedManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    manifest_strict = False
