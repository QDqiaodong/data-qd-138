from .common import require,number,rows
from .components import label

def analyze(data):
    require(data,'connectivity grid');connectivity=number(data['connectivity'],4,8)
    if connectivity not in (4,8):raise ValueError('connectivity must be 4 or 8')
    grid=rows(data['grid'],500)
    if not grid or not isinstance(grid[0],list) or not grid[0] or len(grid[0])>500:raise ValueError('nonempty bounded raster required')
    width=len(grid[0])
    if width*len(grid)>100000:raise ValueError('raster too large')
    for row in grid:
        if not isinstance(row,list) or len(row)!=width:raise ValueError('ragged raster')
        for value in row:
            if value is not None:number(value,0,1)
    return label(grid,connectivity)
