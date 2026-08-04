from setuptools import find_packages, setup

package_name = 'unilabos'

setup(
    name=package_name,
    version='0.11.3',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'setuptools',
        'rfc8785>=0.1.4,<0.2',
        'msgcenterpy>=0.1.8,<0.2',
        'pylabrobot==0.2.1',
    ],
    extras_require={
        'observability': [
            'arize-phoenix==17.5.0',
            'arize-phoenix-otel==0.16.1',
            # Phoenix 17.5.0 尚未兼容 pydantic-ai 2.x，但上游元数据未设置上界。
            'pydantic-ai-slim==1.107.1',
        ],
    },
    zip_safe=True,
    author="The unilabos developers",
    maintainer='Junhan Chang, Xuwznln',
    maintainer_email='Junhan Chang <changjh@pku.edu.cn>, Xuwznln <18435084+Xuwznln@users.noreply.github.com>',
    description='',
    license='GPL v3',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            "unilab = unilabos.app.main:main",
            "unilab-supervisor = unilabos.managed_runtime.supervisor:main",
        ],
    },
)
